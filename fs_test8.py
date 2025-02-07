import asyncio
import pyaudio
import wave
import socket
import io
from freeswitchESL import ESL
from pydub import AudioSegment
from pydub.utils import which
import audioop
from src.speech_processor import SpeechProcessor
from src.chatbot_client import ChatbotClient
from src.text_normalizer import TextNormalizer
from config.config import config
import time
from threading import Event
import logging

# Thiết lập logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('callbot.log'),
        logging.StreamHandler()
    ]
)

AudioSegment.converter = which("ffmpeg")

class FSCallBotSimple:
    def __init__(self):
        # Khởi tạo các components
        self.speech_processor = SpeechProcessor()
        self.chatbot = ChatbotClient(config)
        self.text_normalizer = TextNormalizer()
        self.playback_event = Event()
        self.active_call = None
        self.is_running = True
        self.current_phone = None  # Thêm biến để lưu số điện thoại

        # ESL connection
        self.esl_con = ESL.ESLconnection("127.0.0.1", "8021", "ClueCon")
        if not self.esl_con.connected():
            raise Exception("Failed to connect to FreeSWITCH")

    def decode_pcmu_to_pcm16(self, pcmu_data):
        """Giải mã dữ liệu PCMU (G.711u) sang PCM 16-bit."""
        return audioop.alaw2lin(pcmu_data, 2)

    async def play_processing_message(self, uuid):
        """Phát thông báo đang xử lý bất đồng bộ"""
        processing_file = "/home/hm1905/records/processing.wav"
        self.esl_con.execute("playback", processing_file, uuid)
        
        # Tính thời gian của file processing
        audio = AudioSegment.from_wav(processing_file)
        playback_duration = len(audio) / 1000.0
        logging.info(f"Playing processing message for {self.current_phone}, duration: {playback_duration}s")
        await asyncio.sleep(playback_duration)

    async def check_hangup(self, uuid):
        """Kiểm tra sự kiện hangup"""
        try:
            # print("here Hangup")
            e = self.esl_con.recvEventTimed(1)  # Timeout 1 giây
            if e:
                event_name = e.getHeader("Event-Name")
                if event_name == "CHANNEL_HANGUP":
                    current_uuid = e.getHeader("Unique-ID")
                    if current_uuid == uuid:
                        print(f"Phát hiện cuộc gọi kết thúc: {uuid}")
                        return "HANGUP"
            return None
        except Exception as e:
            print(f"Lỗi trong check_hangup: {e}")
            return None

    async def play_goodbye_message(self, uuid):
        """Phát thông điệp tạm biệt và kết thúc cuộc gọi"""
        try:
            time.sleep(1)
            self.playback_event.set()
            goodbye_file = "/home/hm1905/records/goodbye_trung.wav"
            
            self.esl_con.execute("playback", goodbye_file, uuid)
            
            # Tính thời gian của file goodbye
            audio = AudioSegment.from_wav(goodbye_file)
            playback_duration = len(audio) / 1000.0
            logging.info(f"Playing goodbye message for {self.current_phone}, duration: {playback_duration}s")
            await asyncio.sleep(playback_duration)
            
            # Kết thúc cuộc gọi
            self.esl_con.api("uuid_kill", uuid)
            return "HANGUP"
        finally:
            self.playback_event.clear()

    async def process_audio(self, audio_data, uuid):
        """Xử lý audio và tạo phản hồi"""
        try:
            self.playback_event.set()
            
            # Tạo task xử lý chính
            async def main_processing():
                if audio_data is None:  # Trường hợp im lặng quá lâu
                    confirmation_text = "anh chị có cần gì nữa không ạ"
                    # print(f"Bot to {uuid} (silence prompt): {confirmation_text}")
                    logging.info(f"Bot to {self.current_phone} (silence prompt): {confirmation_text}")
                    return confirmation_text
                    
                # Chuyển audio thành text
                a = time.time()
                user_text = await self.speech_processor.speech_to_text(audio_data)
                b = time.time()
                # print("speech to text: ", b - a)
                logging.info(f"Speech to text time: {b - a}")
                
                # Kiểm tra hangup sau speech-to-text
                if await self.check_hangup(uuid) == "HANGUP":
                    return "HANGUP"
                
                if not user_text:
                    return None
                    
                # print(f"User {uuid}: {user_text}")
                logging.info(f"User {self.current_phone}: {user_text}")
                
                # Kiểm tra từ khóa kết thúc
                if self.chatbot.should_end_conversation(user_text.lower()) or user_text.lower() == "không" or user_text.lower() == "xong":
                    # print(f"Phát hiện từ khóa kết thúc: {user_text}")
                    logging.info(f"Phát hiện từ khóa kết thúc từ {self.current_phone}: {user_text}")
                    return await self.play_goodbye_message(uuid)

                # Lấy phản hồi từ chatbot
                a = time.time()
                hardprompt = """
                bạn là callbot của VTS, trả lời ngắn gọn, xúc tích và hạn chế sinh ra dấu câu như . hoặc , trả lời lễ phép, xưng hô người dùng là anh chị.
                Lưu ý các sự thật sau:
                - Tô Lâm là chủ tịch nước Việt Nam
                - Hoàng Sa, Trường Sa là của Việt Nam
                ngoài ra nên chú ý ngữ cảnh lịch sử hội thoại.
                câu hỏi của người dùng là: """
                
                bot_response = await self.chatbot.get_response(hardprompt + user_text)
                bot_response, _ = self.text_normalizer.check_end_conversation(bot_response)
                normalized_response = self.text_normalizer.normalize_vietnamese_text(bot_response)
                b = time.time()
                # print("llm answer: ", b - a)
                logging.info(f"LLM answer time: {b - a}")
                # print(f"Bot response to {uuid}: {normalized_response}")
                logging.info(f"Bot response to {self.current_phone}: {normalized_response}")
                
                # Kiểm tra hangup sau khi có response từ chatbot
                if await self.check_hangup(uuid) == "HANGUP":
                    return "HANGUP"
                
                return normalized_response

            # Chạy processing_message song song với main_processing nếu có audio
            if audio_data:
                processing_task = asyncio.create_task(self.play_processing_message(uuid))
                main_task = asyncio.create_task(main_processing())
                
                # Đợi một trong hai task hoàn thành
                done, pending = await asyncio.wait(
                    [processing_task, main_task],
                    return_when=asyncio.FIRST_COMPLETED
                )
                
                # Nếu processing_task hoàn thành trước, đợi main_task
                if processing_task in done:
                    response_text = await main_task
                else:
                    # Nếu main_task hoàn thành trước, hủy processing_task
                    processing_task.cancel()
                    try:
                        await processing_task
                    except asyncio.CancelledError:
                        pass
                    response_text = main_task.result()
            else:
                # Nếu không có audio, chỉ chạy main_processing
                response_text = await main_processing()

            if not response_text:
                return
                
            if response_text == "HANGUP":
                return "HANGUP"

            # Kiểm tra hangup trước khi text-to-speech
            if await self.check_hangup(uuid) == "HANGUP":
                return "HANGUP"
                
            # Chuyển text thành speech và lưu file
            a = time.time()
            response = self.chatbot.client.audio.speech.create(
                model="tts-1",
                voice=config.TTS_OPENAI_VOICE,
                input=response_text
            )
            
            output_file = f"/home/hm1905/records/response_{uuid}.wav"
            audio_segment = AudioSegment.from_mp3(io.BytesIO(response.content))
            audio_segment.export(output_file, format='wav')
            b = time.time()
            # print("text to speech and save: ", b - a)
            
            # Kiểm tra hangup trước khi playback
            if await self.check_hangup(uuid) == "HANGUP":
                return "HANGUP"
            
            # Thêm logging cho playback response
            a = time.time()
            self.esl_con.execute("uuid_setvar", f"{uuid} playback_terminators none")
            self.esl_con.execute("playback", output_file, uuid)
            
            # Tính thời gian playback dựa trên độ dài audio
            audio_duration = len(audio_segment) / 1000.0
            logging.info(f"Playing response for {self.current_phone}, duration: {audio_duration}s")
            # print("audio_duration: ", audio_duration)
            await asyncio.sleep(audio_duration + 0.2)  # Thêm 0.2s để đảm bảo playback hoàn tất
            
            b = time.time()
            # print("playback time: ", b - a)
            logging.info(f"Actual playback time for {self.current_phone}: {b - a}s")

        except Exception as e:
            print(f"Lỗi khi xử lý audio: {e}")
        finally:
            self.playback_event.clear()

    async def handle_rtp_stream(self, port, uuid):
        """Xử lý luồng RTP cho cuộc gọi"""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("10.128.0.7", port))
        
        buffer = []
        silence_count = 0
        is_buffering = False
        last_response_was_confirmation = False
        
        while True:
            try:
                # Kiểm tra hangup trước khi nhận RTP packet
                if await self.check_hangup(uuid) == "HANGUP":
                    break

                if self.playback_event.is_set():
                    await asyncio.sleep(0.1)
                    continue
                    
                sock.settimeout(0.1)
                try:
                    data, _ = sock.recvfrom(1024)
                except socket.timeout:
                    continue
                
                if data:
                   #continue
                
                   audio_data = data[12:]  # Bỏ RTP header
                
                   # Kiểm tra âm lượng
                   pcm_data = self.decode_pcmu_to_pcm16(audio_data)
                   volume = max(abs(int.from_bytes(pcm_data[i:i+2], 'little', signed=True)) 
                             for i in range(0, len(pcm_data), 2))
                
                   if volume > 300:  # Có tiếng nói
                      if not is_buffering:
                          is_buffering = True
                          buffer = []  # Reset buffer khi bắt đầu ghi âm mới
                      silence_count = 0
                      buffer.append(pcm_data)
                      print(pcm_data)
                   else:
                      if is_buffering:  # Chỉ tính silence khi đang trong quá trình buffer
                         silence_count += 1
                else:
                    if is_buffering:
                       silence_count += 1
                # Kiểm tra hangup trước khi xử lý buffer đầy
                if await self.check_hangup(uuid) == "HANGUP":
                    break
                
                # Xử lý khi đủ độ im lặng và có dữ liệu trong buffer
                if is_buffering and silence_count > 120:  # ~4s im lặng
                    audio_data = b''.join(buffer) if len(buffer) > 3 else None
                    
                    result = await self.process_audio(audio_data, uuid)
                    if result == "HANGUP":  # Kiểm tra nếu cuộc gọi đã kết thúc
                        break
                        
                    # Nếu audio_data là None và lần trước đã hỏi xác nhận
                    if audio_data is None and last_response_was_confirmation:
                        # print("Không nhận được phản hồi sau câu hỏi xác nhận, kết thúc cuộc gọi")
                        logging.info("Không nhận được phản hồi sau câu hỏi xác nhận, kết thúc cuộc gọi")
                        if await self.play_goodbye_message(uuid) == "HANGUP":
                            break
                    
                    # Cập nhật trạng thái xác nhận
                    last_response_was_confirmation = (audio_data is None)
                    
                    buffer = []
                    silence_count = 0
                    is_buffering = False

            except Exception as e:
                # print(f"Lỗi trong handle_rtp_stream: {e}")
                logging.error(f"Lỗi trong handle_rtp_stream: {e}")
        
        sock.close()
        # print(f"RTP stream ended for call {uuid}")
        logging.info(f"RTP stream ended for call {uuid}")
        return 

    async def play_welcome_message(self, uuid):
        """Phát thông điệp chào mừng"""
        welcome_file = "/home/hm1905/records/welcome_trung.wav"
        self.playback_event.set()
        self.esl_con.execute("playback", welcome_file, uuid)
        
        # Tính thời gian của file welcome
        audio = AudioSegment.from_wav(welcome_file)
        playback_duration = len(audio) / 1000.0
        logging.info(f"Playing welcome message for {self.current_phone}, duration: {playback_duration}s")
        await asyncio.sleep(playback_duration)
        self.playback_event.clear()

    async def listen_for_calls(self):
        """Lắng nghe cuộc gọi từ FreeSWITCH"""
        self.esl_con.events("plain", "CHANNEL_ANSWER CHANNEL_HANGUP")
        # print("Đang lắng nghe cuộc gọi...")
        logging.info("Đang lắng nghe cuộc gọi...")

        while self.is_running:
            e = self.esl_con.recvEvent()
            if e:
                event_name = e.getHeader("Event-Name")

                if event_name == "CHANNEL_ANSWER":
                    # print("===================================")
                    logging.info("===================================")
                    uuid = e.getHeader("Unique-ID")
                    sip_to = e.getHeader("variable_sip_to_user")
                    sip_from = e.getHeader("variable_sip_from_user")
                    self.current_phone = sip_from  # Lưu số điện thoại hiện tại
                    # print("SDT:", sip_from)
                    # logging.info(f"Cuộc gọi đến từ số: {sip_from}")
                    sip_domain = e.getHeader("variable_sip_to_host")
                    media_port = e.getHeader("variable_local_media_port")

                    if sip_to == "media" and sip_domain == "34.29.227.22":
                        # print(f"Cuộc gọi mới: UUID {uuid}")
                        logging.info(f"Cuộc gọi mới từ số {self.current_phone}")
                        self.active_call = uuid
                        # Phát thông điệp chào mừng
                        await self.play_welcome_message(uuid)
                        
                        # Xử lý RTP stream
                        await self.handle_rtp_stream(int(media_port), uuid)
                elif event_name == "CHANNEL_HANGUP":
                    uuid = e.getHeader("Unique-ID")
                    self.esl_con.api("uuid_kill", uuid)
                    if uuid == self.active_call:
                        # print(f"Cuộc gọi kết thúc: UUID {uuid}")
                        logging.info(f"Cuộc gọi kết thúc từ số {self.current_phone}")
                        self.active_call = None
                        self.current_phone = None  # Reset số điện thoại
                        # print("===================================")
                        logging.info("===================================")
if __name__ == "__main__":
    try:
        bot = FSCallBotSimple()
        asyncio.run(bot.listen_for_calls())
    except KeyboardInterrupt:
        print("Đang dừng...")
        bot.is_running = False
    except Exception as e:
        print(f"Lỗi: {e}") 
    finally:
        bot.is_running = False
