import asyncio
import pyaudio
import wave
import socket
import io
import audioop
import time
import logging
# import concurrent.futures
from constants import HANGUP, WELCOME_FILE, GOODBYE_FILE, PROCESSING_FILE, RESPONSE_FILE_TEMPLATE, DEFAULT_SILENCE_PROMPT, CALLBOT_PROMPT
from freeswitchESL import ESL
from pydub import AudioSegment
from pydub.utils import which
from src.speech_processor import SpeechProcessor
from src.chatbot_client import ChatbotClient
from src.text_normalizer import TextNormalizer
from config.config import config
from threading import Event

# Thiết lập logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('callbot.log'),
        logging.StreamHandler()
    ]
)

# Cấu hình ffmpeg cho pydub
AudioSegment.converter = which("ffmpeg")

# [New] Thread pool cho các tác vụ blocking
# executor = concurrent.futures.ThreadPoolExecutor(max_workers=10)

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
        self.esl_con.execute("playback", PROCESSING_FILE, uuid)
        
        # Tính thời gian của file processing
        audio = AudioSegment.from_wav(PROCESSING_FILE)
        playback_duration = len(audio) / 1000.0
        logging.info(f"Playing processing message for {self.current_phone}, duration: {playback_duration}s")
        await asyncio.sleep(playback_duration)

    # [Refactor] Hàm kiểm tra sự kiện hangup
    async def check_hangup(self, uuid):
        """Kiểm tra sự kiện hangup"""
        try:
            # print("here Hangup")
            e = self.esl_con.recvEventTimed(1)  # Timeout 1 giây
            
            if e and e.getHeader("Event-Name") == "CHANNEL_HANGUP" and e.getHeader("Unique-ID") == uuid:
                print(f"Phát hiện cuộc gọi kết thúc: {uuid}")
                return True
            
        except Exception as e:
            print(f"Lỗi trong check_hangup: {e}")
            
        return False

    async def play_goodbye_message(self, uuid):
        """Phát thông điệp tạm biệt và kết thúc cuộc gọi"""
        try:
            time.sleep(1)
            self.playback_event.set()
            
            self.esl_con.execute("playback", GOODBYE_FILE, uuid)
            
            # Tính thời gian của file goodbye
            audio = AudioSegment.from_wav(GOODBYE_FILE)
            playback_duration = len(audio) / 1000.0
            logging.info(f"Playing goodbye message for {self.current_phone}, duration: {playback_duration}s")
            await asyncio.sleep(playback_duration)
            
            # Kết thúc cuộc gọi
            self.esl_con.api("uuid_kill", uuid)
            return HANGUP
        finally:
            self.playback_event.clear()

    ## [Refactor] Hàm xử lý audio và tạo phản hồi
    async def process_audio(self, audio_data, uuid):
        """Xử lý audio và tạo phản hồi"""
        try:
            self.playback_event.set()
            
            if not audio_data:
                logging.info(f"Bot to {self.current_phone} (silence prompt): {DEFAULT_SILENCE_PROMPT}")
                return DEFAULT_SILENCE_PROMPT
            
             # Chạy phát file "Vui lòng chờ..." song song với các tác vụ STT & hangup check
            processing_task = asyncio.create_task(self.play_processing_message(uuid))

             # Chạy STT và kiểm tra hangup cùng lúc
            stt_task = asyncio.create_task(self.speech_processor.speech_to_text(audio_data))
            hangup_task = asyncio.create_task(self.check_hangup(uuid))
            
            user_text, hangup_status = await asyncio.gather(stt_task, hangup_task)

             # Hủy processing_task nếu STT hoàn tất trước khi phát xong
            if not processing_task.done():
                processing_task.cancel()
                try:
                    await processing_task
                except asyncio.CancelledError:
                    pass

            if hangup_status:
                return "HANGUP"

            if not user_text:
                return None
            
            if self.chatbot.should_end_conversation(user_text.lower()) or user_text.lower() == "không" or user_text.lower() == "xong":
                # print(f"Phát hiện từ khóa kết thúc: {user_text}")
                logging.info(f"Phát hiện từ khóa kết thúc từ {self.current_phone}: {user_text}")
                return await self.play_goodbye_message(uuid)
            
            # Gọi chatbot LLM song song với kiểm tra hangup
            chatbot_task = asyncio.create_task(self.chatbot.get_response(CALLBOT_PROMPT + user_text))
            chatbot_response, hangup_status = await asyncio.gather(chatbot_task, self.check_hangup(uuid))

            if hangup_status:
                return "HANGUP"
            
            bot_response, _ = self.text_normalizer.check_end_conversation(chatbot_response)
            normalized_response = self.text_normalizer.normalize_vietnamese_text(bot_response)

            return await self.generate_speech(normalized_response, uuid)
        except Exception as e:
            print(f"Lỗi khi xử lý audio: {e}")
        finally:
            self.playback_event.clear()

    async def generate_speech(self, text, uuid):
        """Chuyển văn bản thành audio và phát lại"""
   
        response = await self.chatbot.client.audio.speech.create(
            model="tts-1",
            voice=config.TTS_OPENAI_VOICE,
            input=text
        )

        output_file = RESPONSE_FILE_TEMPLATE.format(uuid=uuid)
        audio_segment = AudioSegment.from_mp3(io.BytesIO(response.content))
        audio_segment.export(output_file, format="wav")

        self.esl_con.execute("uuid_setvar", f"{uuid} playback_terminators none")
        self.esl_con.execute("playback", output_file, uuid)

        await asyncio.sleep(len(audio_segment) / 1000.0)

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
                if await self.check_hangup(uuid):
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
                if await self.check_hangup(uuid):
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
        welcome_file = WELCOME_FILE
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
