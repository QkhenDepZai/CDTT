"""
Kiểm tra nhanh model Gemini nào ĐANG HOẠT ĐỘNG với API key trong .env.

    python check_gemini_models.py

Gửi 1 câu hỏi rất ngắn tới từng model "flash" mà tài khoản dùng được, in ra
OK / mã lỗi và thời gian phản hồi. Dùng kết quả để điền .env:
    GEMINI_MODEL_NAME            = model OK nhanh nhất
    GEMINI_FALLBACK_MODEL_NAME   = model OK thứ hai
    GEMINI_MODERATION_MODEL_NAME = model OK (ưu tiên bản "lite")
"""
import sys
import time

from google import genai
from google.genai import errors, types

from config import GEMINI_API_KEY


def main():
    if not GEMINI_API_KEY:
        print("[LỖI] Chưa có GEMINI_API_KEY trong .env")
        return 1

    client = genai.Client(
        api_key=GEMINI_API_KEY,
        http_options=types.HttpOptions(timeout=30000, retry_options=types.HttpRetryOptions(attempts=1)),
    )
    models = sorted(
        m.name.split("/")[-1] for m in client.models.list()
        if "flash" in m.name and "generateContent" in (m.supported_actions or [])
        and not any(x in m.name for x in ("image", "tts", "audio", "live"))
    )
    if not models:
        print("[LỖI] Không tìm thấy model flash nào cho API key này.")
        return 1

    print(f"Đang thử {len(models)} model...\n")
    working = []
    for name in models:
        started = time.perf_counter()
        try:
            response = client.models.generate_content(
                model=name, contents="Trả lời đúng 1 từ: OK",
                config=types.GenerateContentConfig(max_output_tokens=20),
            )
            ms = int((time.perf_counter() - started) * 1000)
            print(f"  [OK ] {name:<40} {ms:>6} ms  -> {(response.text or '').strip()[:20]}")
            working.append((ms, name))
        except errors.APIError as exc:
            print(f"  [LỖI] {name:<40} {exc.code} {exc.status}")
        except Exception as exc:  # noqa: BLE001
            print(f"  [LỖI] {name:<40} {type(exc).__name__}")

    if not working:
        print("\nKhông model nào phản hồi. Đợi vài phút rồi thử lại, hoặc kiểm tra quota tại "
              "https://aistudio.google.com")
        return 1

    working.sort()
    print("\nGợi ý điền vào .env:")
    print(f"GEMINI_MODEL_NAME={working[0][1]}")
    print(f"GEMINI_FALLBACK_MODEL_NAME={working[1][1] if len(working) > 1 else working[0][1]}")
    lite = [n for _, n in working if "lite" in n]
    print(f"GEMINI_MODERATION_MODEL_NAME={lite[0] if lite else working[0][1]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
