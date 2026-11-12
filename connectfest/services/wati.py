import os
import uuid
import time
import requests
import frappe
from frappe.utils import flt, now_datetime
from connectfest.services.common import extract_request_data, handle_api_errors

DEFAULT_WATI_HOST = "https://live-mt-server.wati.io"


def get_wati_config():
	"""
	Retrieves WATI configuration safely from:
	1. frappe.conf (site_config.json)
	2. ConnectFest Settings doctype (if available)
	3. Environment variables
	"""
	host = (
		frappe.conf.get("wati_api_host")
		or os.environ.get("WATI_API_HOST")
		or DEFAULT_WATI_HOST
	).rstrip("/")

	token = (
		frappe.conf.get("wati_api_token")
		or frappe.conf.get("wati_api_key")
		or os.environ.get("WATI_API_TOKEN")
		or os.environ.get("WATI_API_KEY")
	)

	template_name = (
		frappe.conf.get("wati_template_name")
		or os.environ.get("WATI_TEMPLATE_NAME")
		or ""
	)

	# Optional override from ConnectFest Settings DocType if exists
	try:
		if frappe.db.exists("DocType", "ConnectFest Settings"):
			doc = frappe.get_cached_doc("ConnectFest Settings")
			if getattr(doc, "wati_api_token", None):
				token = doc.wati_api_token
			if getattr(doc, "wati_api_host", None):
				host = doc.wati_api_host.rstrip("/")
			if getattr(doc, "wati_template_name", None):
				template_name = doc.wati_template_name
	except Exception:
		pass

	return {
		"host": host,
		"token": token,
		"template_name": (template_name or "").strip(),
	}


def normalize_whatsapp_number(mobile):
	"""
	Normalizes a phone number for WATI WhatsApp messaging:
	- Strips non-digits, leading +, leading zeros
	- For 10-digit Indian numbers, prepends '91'
	- Returns sanitized string e.g. '919840012345'
	"""
	if not mobile:
		return ""
	cleaned = "".join(filter(str.isdigit, str(mobile).strip()))
	if not cleaned:
		return ""

	# If 10 digits (Standard Indian Mobile) -> prepend country code 91
	if len(cleaned) == 10:
		return f"91{cleaned}"
	# If starts with 0 and has 11 digits (e.g. 098400...) -> replace 0 with 91
	if len(cleaned) == 11 and cleaned.startswith("0"):
		return f"91{cleaned[1:]}"
	# If already includes country code (e.g. 91XXXXXXXXXX)
	return cleaned


def get_approved_templates():
	"""
	Fetches list of approved templates from WATI account.
	"""
	config = get_wati_config()
	if not config["token"]:
		return []

	try:
		url = f"{config['host']}/api/ext/v3/messageTemplates"
		headers = {
			"Authorization": f"Bearer {config['token']}",
			"Content-Type": "application/json",
		}
		res = requests.get(url, headers=headers, timeout=10)
		if res.status_code == 200:
			templates = res.json().get("templates", [])
			return [t for t in templates if t.get("status") == "approved"]
	except Exception as e:
		frappe.log_error(title="WATI Get Templates Error", message=str(e))
	return []


def resolve_booking_template(preferred_name=None):
	"""
	Resolves the best available template for stall booking confirmation:
	1. Preferred name if specified and valid.
	2. Configured template in site_config/settings.
	3. Any template containing 'booking', 'connectfest', or 'confirm'.
	4. Fallback to approved welcome template.
	"""
	config = get_wati_config()
	target_name = (preferred_name or config["template_name"] or "").strip()

	approved = get_approved_templates()
	approved_map = {t.get("name"): t for t in approved}

	if target_name and target_name in approved_map:
		return target_name

	# Look for domain-specific templates
	for t in approved:
		tname = t.get("name", "").lower()
		if any(keyword in tname for keyword in ["booking", "stall", "connectfest", "confirmation", "receipt"]):
			return t.get("name")

	# Fallbacks available in WATI
	for fallback in ["welcome_wati_v3", "default_welcome_v2", "welcome"]:
		if fallback in approved_map:
			return fallback

	return target_name or (approved[0]["name"] if approved else None)


def send_wati_template_message(mobile, template_name=None, custom_params=None, broadcast_name=None):
	"""
	Sends an approved WhatsApp Template message via WATI API v3.
	Safe, non-blocking failover with full logging.
	"""
	config = get_wati_config()
	if not config["token"]:
		return {
			"status": "skipped",
			"message": "WATI API token not configured in site_config or settings",
		}

	target = normalize_whatsapp_number(mobile)
	if not target or len(target) < 10:
		return {
			"status": "skipped",
			"message": f"Invalid mobile number for WhatsApp: {mobile}",
		}

	# Resolve template name
	resolved_template = template_name or resolve_booking_template()
	if not resolved_template:
		return {
			"status": "skipped",
			"message": "No approved WATI template found to send",
		}

	url = f"{config['host']}/api/ext/v3/messageTemplates/send"
	headers = {
		"Authorization": f"Bearer {config['token']}",
		"Content-Type": "application/json",
	}

	b_name = (broadcast_name or f"connectfest_booking_{int(time.time())}")[:100]
	local_msg_id = str(uuid.uuid4())

	payload = {
		"template_name": resolved_template,
		"broadcast_name": b_name,
		"recipients": [
			{
				"target": target,
				"local_message_id": local_msg_id,
				"custom_params": custom_params or [],
			}
		],
	}

	try:
		response = requests.post(url, headers=headers, json=payload, timeout=15)
		res_json = {}
		try:
			res_json = response.json()
		except Exception:
			pass

		if response.status_code in [200, 201, 202] and res_json.get("success") is True:
			broadcast_id = res_json.get("broadcast_id")
			recipients = res_json.get("recipients", [])
			rec_errors = recipients[0].get("errors") if recipients else []

			if rec_errors:
				frappe.log_error(
					title="WATI WhatsApp Send Recipient Error",
					message=f"Target: {target}, Template: {resolved_template}, Errors: {rec_errors}",
				)
				return {
					"status": "failed",
					"message": f"Recipient error from WATI: {rec_errors}",
					"target": target,
					"template": resolved_template,
				}

			try:
				frappe.logger("connectfest").info(
					f"[ConnectFest WhatsApp] Sent template '{resolved_template}' to +{target} (Broadcast: {broadcast_id})"
				)
			except Exception:
				pass

			return {
				"status": "success",
				"message": f"WhatsApp template message sent to +{target}",
				"broadcast_id": broadcast_id,
				"template": resolved_template,
				"target": target,
			}
		else:
			err_msg = res_json.get("error") or res_json.get("message") or response.text
			frappe.log_error(
				title="WATI WhatsApp Send Template Error",
				message=f"HTTP {response.status_code}: {err_msg} | Payload: {payload}",
			)
			return {
				"status": "error",
				"http_status": response.status_code,
				"message": str(err_msg),
			}

	except Exception as exc:
		frappe.log_error(
			title="WATI WhatsApp Request Exception",
			message=f"Error sending WhatsApp to {target}: {str(exc)}\n{frappe.get_traceback()}",
		)
		return {"status": "error", "message": str(exc)}


def send_booking_confirmation_whatsapp(registration_id=None, mobile_no=None, **kwargs):
	"""
	Sends WhatsApp booking confirmation message to attendee via WATI.
	Safe wrapper that never disrupts payment or registration flows.
	"""
	try:
		reg_data = kwargs.get("reg_data") or {}

		# Fetch registration record if registration_id is supplied
		if registration_id and not reg_data.get("mobile_no"):
			records = frappe.db.sql("""
				SELECT name, registration_id, full_name, company_name, email, mobile_no,
				       stall_number, stall_category, amount, payment_status, payment_mode,
				       razorpay_payment_id, payment_date
				FROM `tabConnectFest Registration`
				WHERE registration_id = %(id)s OR name = %(id)s
				LIMIT 1
			""", {"id": registration_id}, as_dict=True)
			if records:
				reg_data.update(records[0])

		# Override with kwargs
		for k, v in kwargs.items():
			if v and k != "reg_data":
				reg_data[k] = v

		mob = normalize_whatsapp_number(mobile_no or reg_data.get("mobile_no"))
		if not mob:
			return {
				"status": "skipped",
				"message": "No valid mobile number found for attendee WhatsApp notification",
			}

		full_name = (reg_data.get("full_name") or "Valued Attendee").strip()
		company_name = (reg_data.get("company_name") or "").strip()
		reg_id = reg_data.get("registration_id") or reg_data.get("name") or "N/A"
		stall_numbers = reg_data.get("stall_numbers") or reg_data.get("stall_number") or "Assigned"
		amount = flt(reg_data.get("amount", 0.0))
		receipt_id = reg_data.get("razorpay_payment_id") or reg_data.get("receipt_id") or "N/A"
		payment_date = reg_data.get("payment_date") or now_datetime().strftime("%d %b %Y, %I:%M %p")

		# Prepare rich parameter list covering both named & indexed placeholders
		custom_params = [
			{"name": "name", "value": full_name},
			{"name": "full_name", "value": full_name},
			{"name": "company_name", "value": company_name},
			{"name": "stall_number", "value": str(stall_numbers)},
			{"name": "stall_numbers", "value": str(stall_numbers)},
			{"name": "amount", "value": f"₹{amount:,.2f}"},
			{"name": "registration_id", "value": str(reg_id)},
			{"name": "receipt_id", "value": str(receipt_id)},
			{"name": "payment_date", "value": str(payment_date)},
			# Positional variables for Meta templates ({{1}}, {{2}}, {{3}}, ...)
			{"name": "1", "value": full_name},
			{"name": "2", "value": str(stall_numbers)},
			{"name": "3", "value": f"₹{amount:,.2f}"},
			{"name": "4", "value": str(reg_id)},
			{"name": "5", "value": str(receipt_id)},
		]

		result = send_wati_template_message(
			mobile=mob,
			custom_params=custom_params,
			broadcast_name=f"connectfest_stall_{reg_id}_{int(time.time())}",
		)
		return result

	except Exception as e:
		frappe.log_error(title="ConnectFest WhatsApp Send Error", message=frappe.get_traceback())
		return {"status": "error", "message": str(e)}


@frappe.whitelist(allow_guest=True)
@handle_api_errors("ConnectFest Resend WhatsApp Error")
def resend_booking_whatsapp(registration_id=None, mobile_no=None, **kwargs):
	"""
	Public API endpoint to explicitly resend booking confirmation WhatsApp message.
	"""
	data = extract_request_data(kwargs)
	reg_id = (registration_id or data.get("registration_id") or "").strip()
	mob = (mobile_no or data.get("mobile_no") or data.get("mobile") or "").strip()

	if not reg_id and not mob:
		frappe.local.response["http_status_code"] = 400
		return {"status": "error", "message": "registration_id or mobile_no is required"}

	return send_booking_confirmation_whatsapp(registration_id=reg_id, mobile_no=mob)


@frappe.whitelist()
def test_wati_connection():
	"""
	Admin testing utility to verify WATI API connection and list available templates.
	"""
	config = get_wati_config()
	if not config["token"]:
		return {"status": "error", "message": "WATI API token is missing"}

	templates = get_approved_templates()
	return {
		"status": "success",
		"host": config["host"],
		"token_configured": bool(config["token"]),
		"configured_template": config["template_name"],
		"approved_templates_count": len(templates),
		"approved_templates": [t.get("name") for t in templates],
	}

