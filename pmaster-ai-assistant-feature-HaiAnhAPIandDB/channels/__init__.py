"""
Tích hợp đa kênh (Giai đoạn 4): Facebook Messenger, Zalo OA.

    base.py        IncomingMessage, ChannelAdapter, tiện ích text/HTTP
    messenger.py   Facebook Messenger (Graph API Send API)
    zalo.py        Zalo OA (Open API v3, OAuth v4 + tự làm mới token)
    media.py       tải ảnh người dùng gửi từ nền tảng
    dispatcher.py  chống trùng, hàng đợi, gọi chat_service, gửi trả lời
    registry.py    tra adapter theo tên kênh
    repository.py  SQL: identity, phiên theo kênh, sự kiện đã nhận, token

Website: dùng thẳng REST API /api/chat* (widget gọi trực tiếp, không cần webhook).
"""
