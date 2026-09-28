"""
Webhook nhận tin nhắn từ Facebook Messenger và Zalo OA.

    GET  /webhooks/messenger   xác minh đăng ký webhook (hub.challenge)
    POST /webhooks/messenger   sự kiện Messenger (bắt buộc X-Hub-Signature-256)
    POST /webhooks/zalo        sự kiện Zalo OA   (bắt buộc X-ZEvent-Signature)

Quy tắc chung:
- Kênh chưa cấu hình đủ khoá -> 503 (không bao giờ bỏ qua xác thực chữ ký).
- Sai chữ ký -> 403, không xử lý gì.
- Hợp lệ -> chống trùng + xếp hàng xử lý nền, trả 200 NGAY (nền tảng yêu cầu
  phản hồi nhanh, nếu không sẽ gửi lại/khoá webhook).
- Webhook phải là HTTPS công khai: khi dev dùng ngrok / cloudflared tunnel.
"""
import json
import logging

from flask import Blueprint, jsonify, request

from channels.dispatcher import enqueue_events
from channels.registry import get_adapter

logger = logging.getLogger("pmaster.webhooks")

webhooks_bp = Blueprint('webhooks', __name__)


def _configured_adapter(name):
    adapter = get_adapter(name)
    if adapter is None or not adapter.is_configured:
        logger.warning("[Webhook] Nhận webhook %s nhưng kênh chưa cấu hình trong .env", name)
        return None
    return adapter


def _parse_json(raw_body):
    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _handle(name, adapter, raw_body, payload):
    events = adapter.parse_events(payload)
    accepted = enqueue_events(adapter, events) if events else 0
    logger.info("[Webhook] %s: %s sự kiện, %s mới được xếp hàng", name, len(events), accepted)
    return accepted


@webhooks_bp.route('/webhooks/messenger', methods=['GET'])
def messenger_verify():
    adapter = _configured_adapter("messenger")
    if adapter is None:
        return "Messenger channel is not configured", 503
    challenge = adapter.verify_subscription(
        request.args.get("hub.mode", ""),
        request.args.get("hub.verify_token", ""),
        request.args.get("hub.challenge", ""),
    )
    if challenge is None:
        return "Verification failed", 403
    return challenge, 200, {"Content-Type": "text/plain; charset=utf-8"}


@webhooks_bp.route('/webhooks/messenger', methods=['POST'])
def messenger_webhook():
    adapter = _configured_adapter("messenger")
    if adapter is None:
        return jsonify({"error": "Messenger channel is not configured"}), 503

    raw_body = request.get_data(cache=True)
    if not adapter.verify_request(raw_body, request.headers):
        logger.warning("[Webhook] Messenger: sai chữ ký, từ chối (ip=%s)", request.remote_addr)
        return jsonify({"error": "Invalid signature"}), 403

    payload = _parse_json(raw_body)
    if payload is None:
        return jsonify({"error": "Invalid JSON"}), 400
    try:
        _handle("messenger", adapter, raw_body, payload)
    except Exception:  # noqa: BLE001 - trả 500 để Meta gửi lại sau, không mất tin
        logger.exception("[Webhook] Messenger: lỗi khi xếp hàng sự kiện")
        return jsonify({"error": "Internal error"}), 500
    return "EVENT_RECEIVED", 200


@webhooks_bp.route('/webhooks/zalo', methods=['POST'])
def zalo_webhook():
    adapter = _configured_adapter("zalo")
    if adapter is None:
        return jsonify({"error": "Zalo channel is not configured"}), 503

    raw_body = request.get_data(cache=True)
    payload = _parse_json(raw_body)
    if payload is None:
        return jsonify({"error": "Invalid JSON"}), 400
    # Chữ ký Zalo dùng app_id + timestamp nằm TRONG payload nên phải parse trước.
    if not adapter.verify_request(raw_body, request.headers, payload):
        logger.warning("[Webhook] Zalo: sai chữ ký, từ chối (ip=%s)", request.remote_addr)
        return jsonify({"error": "Invalid signature"}), 403
    try:
        _handle("zalo", adapter, raw_body, payload)
    except Exception:  # noqa: BLE001
        logger.exception("[Webhook] Zalo: lỗi khi xếp hàng sự kiện")
        return jsonify({"error": "Internal error"}), 500
    return jsonify({"status": "ok"}), 200
