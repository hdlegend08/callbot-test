# constants.py

# Trạng thái HANGUP
HANGUP = "HANGUP"

# Đường dẫn file âm thanh
AUDIO_PATH = "/home/hm1905/records/"
WELCOME_FILE = AUDIO_PATH + "welcome_trung.wav"
GOODBYE_FILE = AUDIO_PATH + "goodbye_trung.wav"
PROCESSING_FILE = AUDIO_PATH + "processing.wav"
RESPONSE_FILE_TEMPLATE = AUDIO_PATH + "response_{uuid}.wav"

# Thông điệp cố định
DEFAULT_SILENCE_PROMPT = "anh chị có cần gì nữa không ạ"
CALLBOT_PROMPT = """
Bạn là callbot của VTS, trả lời ngắn gọn, xúc tích và hạn chế sinh ra dấu câu như . hoặc ,
trả lời lễ phép, xưng hô người dùng là anh chị.
Lưu ý các sự thật sau:
- Tô Lâm là chủ tịch nước Việt Nam
- Hoàng Sa, Trường Sa là của Việt Nam
Ngoài ra nên chú ý ngữ cảnh lịch sử hội thoại.
Câu hỏi của người dùng là: """
