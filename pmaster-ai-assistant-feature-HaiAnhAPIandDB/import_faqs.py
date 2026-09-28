"""
Import dữ liệu FAQ từ file Excel (danh_sach_faq.xlsx) vào cơ sở dữ liệu MySQL.
Tự động xử lý GỘP Ô (Merged cells) cho các cột PHÂN LOẠI (Intent, Tình huống, Xử lý...).
"""

from urllib.parse import quote_plus

import pandas as pd
from sqlalchemy import create_engine, text

from config import DB_CONFIG

# ======================= CẤU HÌNH =======================
EXCEL_PATH = "danh_sach_faq.xlsx"   # Đường dẫn tới file Excel
SHEET_NAME = "Trang tính1"          # Tên sheet trong file
TABLE_NAME = "faqs"                 # Tên bảng trong MySQL

# Lấy thông tin kết nối từ .env (qua config.py) thay vì hard-code mật khẩu trong code -
# tránh commit nhầm mật khẩu thật lên Git dù .gitignore đã loại trừ .env.
DB_URL = "mysql+pymysql://{user}:{password}@{host}/{database}".format(
    user=quote_plus(DB_CONFIG["user"] or ""),
    password=quote_plus(DB_CONFIG["password"] or ""),
    host=DB_CONFIG["host"],
    database=DB_CONFIG["database"],
)
# ==========================================================

def read_faq_excel(path: str, sheet_name: str) -> pd.DataFrame:
    """Đọc file Excel FAQ và tự động điền dữ liệu cho các ô PHÂN LOẠI bị gộp (merged cells)."""
    df = pd.read_excel(path, sheet_name=sheet_name, header=0)

    # 1. Đặt lại tên cột khớp với MySQL
    df.columns = [
        "stt",
        "nhom_nghiep_vu",
        "intent",
        "tinh_huong",
        "cau_hoi_mau",
        "xu_ly_tinh_huong",
        "tra_loi_chuan",
    ][: len(df.columns)]

    # 2. Xử lý Merged Cells: chỉ áp dụng cho các cột PHÂN LOẠI/NHÓM (hợp lý khi nhiều dòng
    # cùng chung 1 giá trị merge trong Excel). KHÔNG áp dụng cho "tra_loi_chuan" (câu trả lời) -
    # mỗi câu hỏi gần như chắc chắn có câu trả lời RIÊNG, nếu ô đó trống trong Excel thì rất có
    # thể là lỗi nhập liệu, ffill() sẽ vô tình copy nhầm câu trả lời của dòng khác (ví dụ: hỏi
    # "Bảng A" nhưng bị gán câu trả lời của "Bảng B") - thà bỏ sót dòng đó (bị lọc ở bước 3)
    # còn hơn đưa thông tin sai cho thí sinh.
    merged_cols = ["stt", "nhom_nghiep_vu", "intent", "tinh_huong", "xu_ly_tinh_huong"]
    for col in merged_cols:
        if col in df.columns:
            df[col] = df[col].ffill()

    # 3. Lọc bỏ các dòng hoàn toàn trống câu hỏi hoặc câu trả lời (sau khi đã fill các cột phân loại)
    df = df.dropna(subset=["cau_hoi_mau", "tra_loi_chuan"])

    # 4. Thêm cột trạng thái is_active
    df["is_active"] = 1

    # 5. Chuẩn hoá kiểu dữ liệu và xóa khoảng trắng thừa ở hai đầu
    df["stt"] = df["stt"].fillna(0).astype(int)
    for col in ["nhom_nghiep_vu", "intent", "tinh_huong",
                "cau_hoi_mau", "xu_ly_tinh_huong", "tra_loi_chuan"]:
        df[col] = df[col].astype(str).str.strip()

    return df.reset_index(drop=True)


def import_to_db(df: pd.DataFrame, db_url: str, table_name: str):
    """Đẩy dữ liệu đã được làm sạch vào MySQL."""
    engine = create_engine(db_url)

    with engine.connect() as conn:
        # Xóa dữ liệu cũ để nạp bản mới chuẩn nhất
        conn.execute(text(f"TRUNCATE TABLE {table_name}"))
        conn.commit()
        print(f"🧹 Đã dọn dẹp dữ liệu cũ trong bảng '{table_name}'.")

    # Nạp dữ liệu vào MySQL
    df.to_sql(table_name, engine, if_exists="append", index=False)

    with engine.connect() as conn:
        count = conn.execute(text(f"SELECT COUNT(*) FROM {table_name}")).scalar()
    print(f"✅ Import thành công! Bảng '{table_name}' hiện có {count} dòng đầy đủ thông tin.")


def main():
    print(f"Đang đọc dữ liệu từ '{EXCEL_PATH}' ...")
    try:
        df = read_faq_excel(EXCEL_PATH, SHEET_NAME)
        print(f"Đã xử lý xong các ô gộp cho {len(df)} dòng FAQ.")

        print(f"\nĐang kết nối & import vào MySQL...")
        import_to_db(df, DB_URL, TABLE_NAME)
    except Exception as e:
        print(f"❌ Xảy ra lỗi: {e}")


if __name__ == "__main__":
    main()
