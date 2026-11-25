import random
import time
from concurrent.futures import ThreadPoolExecutor
import frappe
from connectfest.services.common import extract_request_data, check_rate_limit, parse_boolean

OTP_CACHE_PREFIX = "connectfest:otp:"
EMAIL_OTP_CACHE_PREFIX = "connectfest:otp:email:"
EMAIL_VERIFIED_CACHE_PREFIX = "connectfest:email_verified:"
OTP_ATTEMPTS_PREFIX = "connectfest:otp_attempts:"
OTP_SEND_COUNT_PREFIX = "connectfest:otp_send_count:"
OTP_BLOCK_PREFIX = "connectfest:otp_block:"
OTP_BLOCK_COUNT_PREFIX = "connectfest:otp_block_count:"
MAX_OTP_ATTEMPTS = 5
MAX_OTP_SEND_REQUESTS = 5
OTP_BLOCK_DURATION_FIRST = 600    # 10 minutes (first block)
OTP_BLOCK_DURATION_SECOND = 1800  # 30 minutes (second/repeat block)
DEFAULT_OTP_EXPIRY_SECONDS = 120  # Default 2 minutes
EMAIL_VERIFIED_EXPIRY_SECONDS = 600   # 10 minutes validity once verified

from connectfest.services.auth import ensure_frappe_user, is_scanner_user

# High-concurrency background email thread pool for zero-lag, instant OTP delivery (1000+ users)
_email_executor = ThreadPoolExecutor(max_workers=20, thread_name_prefix="email_otp_worker")


def get_failed_attempts(email):
	if not email:
		return 0
	cache = frappe.cache()
	key = cache.make_key(f"{OTP_ATTEMPTS_PREFIX}{email}")
	val = cache.get(key)
	if val:
		try:
			return int(val.decode("utf-8") if isinstance(val, bytes) else val)
		except Exception:
			return 0
	return 0


def increment_failed_attempts(email):
	if not email:
		return 1
	cache = frappe.cache()
	key = cache.make_key(f"{OTP_ATTEMPTS_PREFIX}{email}")
	try:
		attempts = cache.incr(key)
	except Exception:
		current = get_failed_attempts(email)
		attempts = current + 1
		cache.set(key, str(attempts))
	cache.expire(key, OTP_BLOCK_DURATION_FIRST)
	return int(attempts)


def get_otp_send_count(email):
	if not email:
		return 0
	cache = frappe.cache()
	key = cache.make_key(f"{OTP_SEND_COUNT_PREFIX}{email}")
	val = cache.get(key)
	if val:
		try:
			return int(val.decode("utf-8") if isinstance(val, bytes) else val)
		except Exception:
			return 0
	return 0


def increment_otp_send_count(email):
	if not email:
		return 1
	cache = frappe.cache()
	key = cache.make_key(f"{OTP_SEND_COUNT_PREFIX}{email}")
	try:
		count = cache.incr(key)
	except Exception:
		current = get_otp_send_count(email)
		count = current + 1
		cache.set(key, str(count))
	cache.expire(key, 600)  # 10 minutes window
	return int(count)


def trigger_block(email):
	"""
	Sets temporary progressive block for an email:
	1st lockout: 10 minutes (600s)
	2nd or subsequent lockout: 30 minutes (1800s)
	"""
	if not email:
		return 10
	cache = frappe.cache()
	count_key = cache.make_key(f"{OTP_BLOCK_COUNT_PREFIX}{email}")
	try:
		block_count = cache.incr(count_key)
	except Exception:
		val = cache.get(count_key)
		block_count = int(val) + 1 if val else 1
		cache.set(count_key, str(block_count))
	cache.expire(count_key, 86400)  # Remember repeat offender within 24 hours

	duration_secs = OTP_BLOCK_DURATION_FIRST if block_count <= 1 else OTP_BLOCK_DURATION_SECOND
	duration_mins = duration_secs // 60

	block_key = cache.make_key(f"{OTP_BLOCK_PREFIX}{email}")
	cache.setex(block_key, duration_secs, str(duration_mins))
	cache.delete(cache.make_key(f"{OTP_ATTEMPTS_PREFIX}{email}"))
	cache.delete(cache.make_key(f"{OTP_SEND_COUNT_PREFIX}{email}"))
	return duration_mins


def clear_failed_attempts(email):
	if not email:
		return
	cache = frappe.cache()
	cache.delete(cache.make_key(f"{OTP_ATTEMPTS_PREFIX}{email}"))
	cache.delete(cache.make_key(f"{OTP_SEND_COUNT_PREFIX}{email}"))
	cache.delete(cache.make_key(f"{OTP_BLOCK_PREFIX}{email}"))


def is_otp_blocked(email):
	if not email:
		return False, 0
	cache = frappe.cache()
	key = cache.make_key(f"{OTP_BLOCK_PREFIX}{email}")
	val = cache.get(key)
	if not val:
		return False, 0
	ttl = cache.ttl(key)
	mins = max(1, (ttl // 60) + (1 if ttl % 60 else 0)) if (ttl and ttl > 0) else 10
	return True, mins


def get_otp_expiry_seconds():
	"""
	Dynamically fetches OTP validity duration in seconds from ConnectFest Settings.
	Defaults to 120 seconds (2 minutes).
	"""
	try:
		val = frappe.db.get_single_value("ConnectFest Settings", "otp_expiry_seconds")
		if val and int(val) > 0:
			return int(val)
	except Exception:
		pass
	return DEFAULT_OTP_EXPIRY_SECONDS


def get_email_verified_validity_seconds():
	"""
	Dynamically fetches how long a verified email remains valid for registration (in seconds).
	Configurable via 'email_verified_validity_minutes' in ConnectFest Settings (defaults to 10 mins = 600s).
	"""
	try:
		mins = frappe.db.get_value("Singles", {"doctype": "ConnectFest Settings", "field": "email_verified_validity_minutes"}, "value")
		if not mins:
			mins = frappe.db.get_single_value("ConnectFest Settings", "email_verified_validity_minutes")
		if mins and int(mins) > 0:
			return int(mins) * 60
	except Exception:
		pass
	return EMAIL_VERIFIED_EXPIRY_SECONDS


def format_expiry_text(seconds):
	"""Returns human-friendly string for OTP expiry duration (e.g. '2 minutes' or '90 seconds')."""
	if seconds % 60 == 0:
		mins = seconds // 60
		return f"{mins} minute{'s' if mins > 1 else ''}"
	return f"{seconds} seconds"


def _background_send_email_task(site_name, recipient, subject, content):
	"""
	Asynchronous background worker:
	Establishes SMTP connection and delivers OTP without stalling the web HTTP worker.
	Ensures web requests respond in <30ms, preventing 504 timeouts and server lag.
	"""
	try:
		frappe.init(site=site_name)
		frappe.connect()

		# Dynamically resolve sender from configured default outgoing Email Account
		outgoing_acc = frappe.db.get_value(
			"Email Account",
			{"default_outgoing": 1},
			["email_id", "email_account_name", "name"],
			as_dict=True,
		)
		sender_str = None
		if outgoing_acc and outgoing_acc.get("email_id"):
			s_name = outgoing_acc.get("email_account_name") or outgoing_acc.get("name") or "ConnectFest"
			sender_str = f"{s_name} <{outgoing_acc.get('email_id')}>"

		sendmail_args = {
			"recipients": [recipient],
			"subject": subject,
			"message": content,
			"now": True,
			"delayed": False,
		}
		if sender_str:
			sendmail_args["sender"] = sender_str

		frappe.sendmail(**sendmail_args)
		frappe.db.commit()
		frappe.logger("connectfest").info(f"[Fast Async Email OTP] Delivered OTP to {recipient}")
	except Exception as e:
		frappe.logger("connectfest").warning(f"[Fast Async Email OTP] Error sending to {recipient}: {str(e)}")
	finally:
		try:
			frappe.destroy()
		except Exception:
			pass


def normalize_mobile(mobile):
	if not mobile:
		return ""
	cleaned = "".join(filter(str.isdigit, str(mobile).strip()))
	if len(cleaned) > 10:
		# If includes country code +91
		cleaned = cleaned[-10:]
	return cleaned


def send_otp(mobile_no=None, **kwargs):
	"""
	Universal Login OTP Request API.
	Routes directly to Email OTP for lag-free, secure email verification and login.
	Accepts email, or resolves registered attendee email from mobile number.
	Strictly requires the attendee to be registered first.
	"""
	data = extract_request_data(kwargs)
	target_email = str(data.get("email") or data.get("user_email") or "").strip().lower()
	mob = normalize_mobile(mobile_no or data.get("mobile_no") or data.get("phone") or data.get("mobile"))

	if not target_email and mob:
		target_email = (
			frappe.db.get_value("ConnectFest Registration", {"mobile_no": mob}, "email")
			or frappe.db.get_value("ConnectFest Visitor Registration", {"mobile_no": mob}, "email")
			or frappe.db.get_value("ConnectFest BNI Event Registration", {"mobile_no": mob}, "email")
			or frappe.db.get_value("User", {"mobile_no": mob}, "name")
			or frappe.db.get_value("User", {"phone": mob}, "name")
			or ""
		)
		if target_email:
			target_email = target_email.strip().lower()
		else:
			frappe.local.response["http_status_code"] = 404
			return {
				"status": "error",
				"error_code": "NOT_REGISTERED",
				"is_registered": False,
				"message": "Mobile number is not registered with ConnectFest! Please register first.",
				"next_action": "register",
			}

	if target_email:
		return send_email_otp(email=target_email, mobile_no=mob, **kwargs)

	frappe.local.response["http_status_code"] = 400
	return {
		"status": "error",
		"message": "A valid email address or registered mobile number is required to receive verification OTP.",
	}


def verify_otp(mobile_no=None, otp=None, **kwargs):
	"""
	Universal Login & OTP Verification API.
	Enforces Email OTP for secure attendee session login.
	Accepts email directly, or resolves registered attendee email from mobile number.
	"""
	data = extract_request_data(kwargs)
	submitted_email = str(data.get("email") or data.get("user_email") or "").strip().lower()
	mob = normalize_mobile(mobile_no or data.get("mobile_no") or data.get("phone") or data.get("mobile"))
	submitted_otp = str(otp or data.get("otp") or data.get("code") or data.get("email_otp") or "").strip()

	if not submitted_email and mob:
		submitted_email = (
			frappe.db.get_value("ConnectFest Registration", {"mobile_no": mob}, "email")
			or frappe.db.get_value("ConnectFest Visitor Registration", {"mobile_no": mob}, "email")
			or frappe.db.get_value("ConnectFest BNI Event Registration", {"mobile_no": mob}, "email")
			or frappe.db.get_value("User", {"mobile_no": mob}, "name")
			or frappe.db.get_value("User", {"phone": mob}, "name")
			or ""
		)
		if submitted_email:
			submitted_email = submitted_email.strip().lower()
		else:
			frappe.local.response["http_status_code"] = 404
			return {
				"status": "error",
				"error_code": "NOT_REGISTERED",
				"is_registered": False,
				"message": "Mobile number is not registered with ConnectFest! Please register first.",
				"next_action": "register",
			}

	if not submitted_otp:
		frappe.local.response["http_status_code"] = 400
		return {"status": "error", "message": "OTP is required"}

	if submitted_email:
		return verify_email_otp(email=submitted_email, otp=submitted_otp, **kwargs)

	frappe.local.response["http_status_code"] = 400
	return {
		"status": "error",
		"message": "Valid email address is required for Email OTP verification.",
	}


def build_email_otp_html(otp_code, target_email=None, user_name=None, expiry_seconds=120):
	"""
	Clean, minimal, human-crafted HTML email template for ConnectFest Email OTP.
	High-deliverability format designed to land directly in Inbox (anti-spam optimized).
	"""
	display_name = (user_name or "").strip()
	greeting = f"Hello {display_name}," if display_name else "Hello,"
	exp_text = format_expiry_text(expiry_seconds)

	html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>ConnectFest Verification Code</title>
</head>
<body style="margin: 0; padding: 0; background-color: #f8fafc; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; -webkit-font-smoothing: antialiased;">
  <div style="max-width: 480px; margin: 30px auto; background-color: #ffffff; border: 1px solid #e2e8f0; border-radius: 12px; overflow: hidden; box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.05);">
    
    <!-- Header -->
    <div style="background-color: #4f46e5; padding: 24px 20px; text-align: center;">
      <div style="font-size: 11px; font-weight: 700; letter-spacing: 2px; text-transform: uppercase; color: #e0e7ff; margin-bottom: 6px;">
        CONNECTFEST 2027
      </div>
      <h1 style="margin: 0; font-size: 20px; font-weight: 700; color: #ffffff; letter-spacing: -0.3px;">
        Email Verification Code
      </h1>
    </div>

    <!-- Body Content -->
    <div style="padding: 28px 24px;">
      <p style="font-size: 15px; color: #0f172a; margin: 0 0 10px; font-weight: 600;">
        {greeting}
      </p>
      <p style="font-size: 15px; color: #475569; margin: 0 0 20px; line-height: 1.5;">
        Your one-time verification code for ConnectFest is:
      </p>

      <!-- Clean OTP Box -->
      <div style="background-color: #f8fafc; border: 1px solid #e2e8f0; border-radius: 8px; padding: 18px 12px; text-align: center; margin: 0 0 20px;">
        <div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Courier New', monospace; font-size: 34px; font-weight: 800; letter-spacing: 8px; color: #0f172a; margin-left: 8px;">
          {otp_code}
        </div>
      </div>

      <p style="font-size: 13px; color: #64748b; margin: 0 0 24px; line-height: 1.4;">
        This OTP is valid for {exp_text}. Please do not share this code with anyone.
      </p>

      <hr style="border: none; border-top: 1px solid #f1f5f9; margin: 0 0 16px;" />

      <p style="font-size: 13px; color: #94a3b8; margin: 0;">
        ConnectFest Team
      </p>
    </div>

  </div>
</body>
</html>"""
	return html


def build_admin_2fa_html(otp_code, admin_user=None, admin_email=None, expiry_seconds=120):
	"""
	Clean, simple, minimal HTML email template for ConnectFest Admin 2FA.
	Anti-spam optimized with preheaders and clean styling so it lands directly in Inbox.
	"""
	display_name = (admin_user or "").strip()
	greeting = f"Hello {display_name}," if display_name and display_name.lower() != "administrator" else "Hello Administrator,"
	exp_text = format_expiry_text(expiry_seconds)

	html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>ConnectFest Admin Verification</title>
</head>
<body style="margin: 0; padding: 0; background-color: #f8fafc; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; -webkit-font-smoothing: antialiased;">
  <!-- Preheader text for clean preview in inbox -->
  <div style="display: none; font-size: 1px; line-height: 1px; max-height: 0px; max-width: 0px; opacity: 0; overflow: hidden; mso-hide: all;">
    Your Admin verification code is {otp_code}. Valid for {exp_text}.
  </div>

  <div style="max-width: 480px; margin: 30px auto; background-color: #ffffff; border: 1px solid #e2e8f0; border-radius: 12px; overflow: hidden; box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.05);">
    
    <!-- Header -->
    <div style="background-color: #4f46e5; padding: 24px 20px; text-align: center;">
      <div style="font-size: 11px; font-weight: 700; letter-spacing: 2px; text-transform: uppercase; color: #e0e7ff; margin-bottom: 6px;">
        CONNECTFEST 2027
      </div>
      <h1 style="margin: 0; font-size: 20px; font-weight: 700; color: #ffffff; letter-spacing: -0.3px;">
        Admin Verification Code
      </h1>
    </div>

    <!-- Body Content -->
    <div style="padding: 28px 24px;">
      <p style="font-size: 15px; color: #0f172a; margin: 0 0 10px; font-weight: 600;">
        {greeting}
      </p>
      <p style="font-size: 14px; color: #475569; margin: 0 0 20px; line-height: 1.5;">
        Your one-time verification code for ConnectFest Admin login is:
      </p>

      <!-- Clean OTP Box -->
      <div style="background-color: #f8fafc; border: 1px solid #e2e8f0; border-radius: 8px; padding: 18px 12px; text-align: center; margin: 0 0 20px;">
        <div style="font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace; font-size: 34px; font-weight: 800; letter-spacing: 8px; color: #0f172a; margin-left: 8px;">
          {otp_code}
        </div>
      </div>

      <p style="font-size: 13px; color: #64748b; margin: 0 0 24px; line-height: 1.4;">
        This code is valid for {exp_text}. Please do not share this code with anyone.
      </p>

      <hr style="border: none; border-top: 1px solid #f1f5f9; margin: 0 0 16px;" />

      <p style="font-size: 12px; color: #94a3b8; margin: 0;">
        ConnectFest Team
      </p>
    </div>

  </div>
</body>
</html>"""
	return html


def is_email_registered(email):
	"""
	Checks whether an email is registered in ConnectFest.
	Inspects:
	1. ConnectFest Registration (Exhibitors)
	2. ConnectFest Visitor Registration (Visitors)
	3. ConnectFest BNI Event Registration (Event Attendees)
	4. Active Frappe User (Admin / Desk Users)
	Returns: (is_registered: bool, info: dict or None)
	"""
	if not email or "@" not in str(email):
		return False, None
	clean_email = str(email).strip().lower()

	# 1. Exhibitor Registration
	exhibitor = frappe.db.get_value(
		"ConnectFest Registration",
		{"email": clean_email},
		["name", "full_name", "registration_id", "company_name", "payment_status"],
		as_dict=True,
	)
	if exhibitor:
		return True, {"type": "Exhibitor", "data": exhibitor, "full_name": exhibitor.get("full_name")}

	# 2. Visitor Registration
	visitor = frappe.db.get_value(
		"ConnectFest Visitor Registration",
		{"email": clean_email},
		["name", "full_name", "registration_id"],
		as_dict=True,
	)
	if visitor:
		return True, {"type": "Visitor", "data": visitor, "full_name": visitor.get("full_name")}

	# 3. BNI Event Registration
	bni_event = frappe.db.get_value(
		"ConnectFest BNI Event Registration",
		{"email": clean_email},
		["name", "full_name", "registration_id", "pass_code"],
		as_dict=True,
	)
	if bni_event:
		return True, {"type": "Event Attendee", "data": bni_event, "full_name": bni_event.get("full_name")}

	# 4. Standard Frappe User (Admin / Desk Users)
	user = frappe.db.sql("""
		SELECT name, full_name, enabled, email
		FROM `tabUser`
		WHERE name = %s OR email = %s
		LIMIT 1
	""", (clean_email, clean_email), as_dict=True)
	if user and user[0].get("enabled"):
		return True, {
			"type": "User",
			"data": user[0],
			"full_name": user[0].get("full_name"),
			"user_id": user[0].get("name"),
		}

	return False, None


def get_user_role_types(email, mobile_no=None, user_id=None):
	"""
	Returns an array of roles/types the user possesses:
	e.g. ['Admin', 'Exhibitor', 'Visitor', 'Event', 'ConnectFest Gallery Admin']
	"""
	roles = []
	from connectfest.services.auth import is_admin_user, get_user_permissions_summary
	clean_email = str(email or "").strip().lower()
	uid = user_id or frappe.db.get_value("User", {"email": clean_email}, "name") or clean_email

	if is_admin_user(uid):
		roles.append("Admin")
	if frappe.db.exists("ConnectFest Registration", {"email": clean_email}) or (mobile_no and frappe.db.exists("ConnectFest Registration", {"mobile_no": mobile_no})):
		roles.append("Exhibitor")
	if frappe.db.exists("ConnectFest Visitor Registration", {"email": clean_email}) or (mobile_no and frappe.db.exists("ConnectFest Visitor Registration", {"mobile_no": mobile_no})):
		roles.append("Visitor")
	if frappe.db.exists("ConnectFest BNI Event Registration", {"email": clean_email}) or (mobile_no and frappe.db.exists("ConnectFest BNI Event Registration", {"mobile_no": mobile_no})):
		roles.append("Event")

	perm_summary = get_user_permissions_summary(uid)
	for r_name in perm_summary["roles"]:
		if r_name not in ("Website User", "System User") and r_name not in roles:
			roles.append(r_name)

	if not roles:
		if perm_summary["is_scanner"]:
			roles.append("Scanner")
		else:
			roles.append("User")

	return roles


def send_email_otp(email=None, **kwargs):
	"""
	Generates and sends a 6-digit OTP to the attendee's email address for LOGIN.
	Stores OTP in Redis and Frappe DocType with dynamic expiry from ConnectFest Settings.
	Strictly requires the email to be registered first.
	Returns is_scanner status and dynamic expiry info.
	"""
	data = extract_request_data(kwargs)
	target_email = str(email or data.get("email") or "").strip().lower()

	if not target_email or "@" not in target_email or "." not in target_email.split("@")[-1]:
		frappe.local.response["http_status_code"] = 400
		return {
			"status": "error",
			"message": "A valid email address is required to receive OTP.",
		}

	from connectfest.services.auth import get_authenticated_user, is_admin_user

	# Validate purpose: auto-detect if missing based on registration status
	purpose = str(data.get("purpose") or kwargs.get("purpose") or "").strip().lower()
	if not purpose:
		is_already_reg, _ = is_email_registered(target_email)
		if not is_already_reg and is_admin_user(target_email):
			is_already_reg = True
		purpose = "login" if is_already_reg else "register"
	elif purpose in ("registration",):
		purpose = "register"

	if purpose not in ("register", "login"):
		purpose = "register"

	# Disallow sending OTP for another user if already logged in
	auth_user = get_authenticated_user(data, **kwargs)
	if auth_user and not is_admin_user(auth_user):
		if purpose == "login":
			frappe.local.response["http_status_code"] = 400
			return {
				"status": "error",
				"message": "You are already logged in! Please log out to request a login OTP.",
			}
		elif purpose == "register":
			if target_email != auth_user.lower():
				frappe.local.response["http_status_code"] = 400
				return {
					"status": "error",
					"message": "You are currently logged in. Verification OTP can only be requested for your active account. Please log out first to register another account.",
				}

	# Check registration status scoped to registration type if provided
	reg_type = str(
		data.get("registration_type")
		or data.get("type")
		or data.get("role")
		or data.get("category")
		or kwargs.get("registration_type")
		or kwargs.get("type")
		or kwargs.get("role")
		or kwargs.get("category")
		or ""
	).strip().lower()

	role_label = "with ConnectFest"
	reg_info = None

	if reg_type in ("visitor", "vis", "visitor_registration"):
		is_reg = bool(frappe.db.exists("ConnectFest Visitor Registration", {"email": target_email}))
		role_label = "as a Visitor"
		if is_reg:
			v_doc = frappe.db.get_value("ConnectFest Visitor Registration", {"email": target_email}, ["name", "full_name"], as_dict=True)
			reg_info = {"type": "Visitor", "data": v_doc, "full_name": v_doc.get("full_name") if v_doc else ""}
	elif reg_type in ("stall", "stalls", "exhibitor", "exhibitors", "stall_registration"):
		is_reg = bool(frappe.db.exists("ConnectFest Registration", {"email": target_email}))
		role_label = "as an Exhibitor"
		if is_reg:
			e_doc = frappe.db.get_value("ConnectFest Registration", {"email": target_email}, ["name", "full_name"], as_dict=True)
			reg_info = {"type": "Exhibitor", "data": e_doc, "full_name": e_doc.get("full_name") if e_doc else ""}
	elif reg_type in ("event", "events", "bni_event", "event_registration"):
		evt_name = data.get("event_name") or data.get("event") or kwargs.get("event_name")
		if evt_name:
			is_reg = bool(frappe.db.exists("ConnectFest BNI Event Registration", {"email": target_email, "event_name": evt_name}))
		else:
			is_reg = bool(frappe.db.exists("ConnectFest BNI Event Registration", {"email": target_email}))
		role_label = "for this event"
		if is_reg:
			evt_doc = frappe.db.get_value("ConnectFest BNI Event Registration", {"email": target_email}, ["name", "full_name"], as_dict=True)
			reg_info = {"type": "Event Attendee", "data": evt_doc, "full_name": evt_doc.get("full_name") if evt_doc else ""}
	else:
		is_reg, reg_info = is_email_registered(target_email)
		role_label = "with ConnectFest"

	# Admin user support for OTP login
	if is_admin_user(target_email):
		is_reg = True
		role_label = "as an Administrator"
		if not reg_info:
			reg_info = {"type": "Administrator", "data": {}, "full_name": "Administrator"}

	if purpose == "login" and not is_reg:
		frappe.local.response["http_status_code"] = 404
		return {
			"status": "error",
			"error_code": "NOT_REGISTERED",
			"is_registered": False,
			"message": f"This email address is not registered {role_label}! Please register first.",
			"next_action": "register",
		}

	if purpose in ("register", "registration") and is_reg:
		frappe.local.response["http_status_code"] = 400
		return {
			"status": "error",
			"error_code": "ALREADY_REGISTERED",
			"already_registered": True,
			"message": f"This email address is already registered {role_label}! Please log in to access your account.",
			"next_action": "login",
		}

	# Check if account is temporarily locked
	blocked, mins_left = is_otp_blocked(target_email)
	if blocked:
		frappe.local.response["http_status_code"] = 429
		unit = "minute" if mins_left == 1 else "minutes"
		return {
			"status": "error",
			"error_code": "ACCOUNT_TEMPORARILY_BLOCKED",
			"is_blocked": True,
			"message": f"Too many OTP requests. Please try again after {mins_left} {unit}.",
			"remaining_requests": 0,
		}

	# Check OTP send count: max 5 requests per 10 minutes
	send_count = increment_otp_send_count(target_email)
	if send_count > MAX_OTP_SEND_REQUESTS:
		duration_mins = trigger_block(target_email)
		frappe.local.response["http_status_code"] = 429
		return {
			"status": "error",
			"error_code": "ACCOUNT_TEMPORARILY_BLOCKED",
			"is_blocked": True,
			"message": f"Too many OTP requests. Your account is temporarily locked for {duration_mins} minutes.",
			"remaining_requests": 0,
		}

	remaining_sends = MAX_OTP_SEND_REQUESTS - send_count

	# Dynamic OTP validity from ConnectFest Settings
	otp_expiry = get_otp_expiry_seconds()
	exp_text = format_expiry_text(otp_expiry)

	# Generate 6-digit secure numeric OTP (no static test OTPs)
	otp_code = f"{random.randint(100000, 999999)}"

	# Retrieve attendee name for personalization if available
	user_name = str(data.get("full_name") or data.get("name") or "").strip()
	if not user_name and is_reg and reg_info and reg_info.get("full_name"):
		user_name = reg_info.get("full_name")

	if not user_name:
		user_name = (
			frappe.db.get_value("ConnectFest Registration", {"email": target_email}, "full_name")
			or frappe.db.get_value("ConnectFest Visitor Registration", {"email": target_email}, "full_name")
			or frappe.db.get_value("User", {"email": target_email}, "full_name")
			or ""
		)

	# 1. Store in Frappe DocType 'ConnectFest OTP' (Persistent in MariaDB for Admin visibility)
	expires_at = frappe.utils.add_to_date(frappe.utils.now_datetime(), seconds=otp_expiry)
	try:
		# Expire previous pending OTPs that have genuinely reached their expiry
		frappe.db.sql("""
			UPDATE `tabConnectFest OTP`
			SET status = 'Expired'
			WHERE email = %s AND status = 'Pending' AND expires_at < %s
		""", (target_email, frappe.utils.now_datetime()))

		otp_doc = frappe.get_doc({
			"doctype": "ConnectFest OTP",
			"email": target_email,
			"mobile_no": str(data.get("mobile_no") or data.get("phone") or ""),
			"registered_user": user_name,
			"otp": otp_code,
			"status": "Pending",
			"expires_at": expires_at,
		})
		otp_doc.flags.ignore_permissions = True
		otp_doc.insert()
		frappe.db.commit()
	except Exception as e:
		frappe.log_error("ConnectFest Email OTP Save Error", str(e))

	# 2. Also cache in Redis for ultra-fast lookup
	cache = frappe.cache()
	cache_key = cache.make_key(f"{EMAIL_OTP_CACHE_PREFIX}{target_email}")
	cache.setex(cache_key, otp_expiry, otp_code)

	# Cache the requested registration role/type for strict matching in verify_email_otp
	normalized_send_type = "any"
	if reg_type in ("visitor", "vis", "visitor_registration"):
		normalized_send_type = "visitor"
	elif reg_type in ("stall", "stalls", "exhibitor", "exhibitors", "stall_registration"):
		normalized_send_type = "stall"
	elif reg_type in ("event", "events", "bni_event", "event_registration"):
		normalized_send_type = "event"

	type_cache_key = cache.make_key(f"connectfest:otp_type:{target_email}")
	cache.setex(type_cache_key, otp_expiry, normalized_send_type)

	# 3. Verified email dispatch with full error tracking
	email_subject = f"{otp_code} is your ConnectFest verification code"
	email_content = build_email_otp_html(otp_code, target_email=target_email, user_name=user_name, expiry_seconds=otp_expiry)

	in_test = getattr(frappe.flags, "in_test", False) or getattr(getattr(frappe, "local", None), "in_test", False)
	has_outgoing = bool(
		frappe.db.get_value("Email Account", {"default_outgoing": 1, "enable_outgoing": 1})
		or frappe.db.get_value("Email Account", {"enable_outgoing": 1})
	)

	if not has_outgoing and not in_test:
		frappe.log_error(
			title="ConnectFest OTP Email Account Missing",
			message=f"Attempted to send OTP to '{target_email}', but no active outgoing Email Account is configured in Frappe (Desk > Email Account)."
		)
		frappe.db.commit()
		frappe.local.response["http_status_code"] = 500
		return {
			"status": "error",
			"message": "Unable to send verification email: No active outgoing Email Account is configured in Frappe (Desk > Email Account).",
			"error": "Email Account missing",
		}

	# Dynamically resolve sender from configured default outgoing Email Account
	outgoing_acc = frappe.db.get_value(
		"Email Account",
		{"default_outgoing": 1, "enable_outgoing": 1},
		["email_id", "email_account_name", "name"],
		as_dict=True,
	) or frappe.db.get_value(
		"Email Account",
		{"enable_outgoing": 1},
		["email_id", "email_account_name", "name"],
		as_dict=True,
	)
	sender_str = None
	if outgoing_acc and outgoing_acc.get("email_id"):
		s_name = outgoing_acc.get("email_account_name") or outgoing_acc.get("name") or "ConnectFest"
		sender_str = f"{s_name} <{outgoing_acc.get('email_id')}>"

	if not in_test:
		try:
			sendmail_args = {
				"recipients": [target_email],
				"subject": email_subject,
				"message": email_content,
				"now": True,
				"delayed": False,
				"x_priority": 1,
				"send_priority": 1,
			}
			if sender_str:
				sendmail_args["sender"] = sender_str

			q = frappe.sendmail(**sendmail_args)
			frappe.db.commit()

			# Verify queue entry status if created
			if q and getattr(q, "name", None):
				q_info = frappe.db.get_value("Email Queue", q.name, ["status", "error"], as_dict=True)
				if q_info and q_info.get("status") == "Error":
					err_detail = q_info.get("error") or "SMTP transmission error"
					frappe.log_error(
						title=f"ConnectFest Email OTP Failed ({target_email})",
						message=f"Email Queue [{q.name}] encountered an error for {target_email}:\n\n{err_detail}"
					)
					frappe.db.commit()
					frappe.local.response["http_status_code"] = 500
					return {
						"status": "error",
						"message": f"Unable to send verification email: {err_detail}",
						"error": err_detail,
					}
		except Exception as send_err:
			frappe.log_error(
				title=f"ConnectFest Email OTP Send Error ({target_email})",
				message=frappe.get_traceback()
			)
			frappe.db.commit()
			frappe.local.response["http_status_code"] = 500
			return {
				"status": "error",
				"message": f"Failed to send verification email: {str(send_err)}",
				"error": str(send_err),
			}

	# Log OTP in Frappe site log for observability
	try:
		frappe.logger("connectfest").info(f"[ConnectFest Email OTP] Sent OTP {otp_code} to {target_email} (Expires in {exp_text})")
	except Exception:
		pass

	uid = (reg_info.get("user_id") if reg_info else None) or frappe.db.get_value("User", {"email": target_email}, "name") or target_email
	is_scanner = bool(is_scanner_user(uid))

	return {
		"status": "success",
		"message": f"Verification code sent successfully to your email address. Please check your inbox (valid for {exp_text}).",
		"purpose": purpose,
		"is_registered": bool(is_reg),
		"expires_in_seconds": otp_expiry,
		"expires_in": exp_text,
		"remaining_requests": remaining_sends,
		"is_scanner": is_scanner,
	}


def verify_email_otp(email=None, otp=None, **kwargs):
	"""
	Verifies the email OTP submitted by the attendee.
	Strictly requires the actual OTP sent via email (NO test master OTPs).
	Upon successful verification, authenticates user into Frappe session and returns roles array.
	"""
	data = extract_request_data(kwargs)
	target_email = str(email or data.get("email") or "").strip().lower()
	submitted_otp = str(otp or data.get("otp") or data.get("code") or data.get("email_otp") or "").strip()

	if not target_email or "@" not in target_email:
		frappe.local.response["http_status_code"] = 400
		return {"status": "error", "message": "Valid email address is required"}

	if not submitted_otp:
		frappe.local.response["http_status_code"] = 400
		return {"status": "error", "message": "OTP is required"}

	# Check if account is temporarily locked due to failed attempts
	blocked, mins_left = is_otp_blocked(target_email)
	if blocked:
		frappe.local.response["http_status_code"] = 429
		unit = "minute" if mins_left == 1 else "minutes"
		return {
			"status": "error",
			"error_code": "ACCOUNT_TEMPORARILY_BLOCKED",
			"is_blocked": True,
			"message": f"Too many incorrect attempts. Please try again after {mins_left} {unit}.",
			"remaining_attempts": 0,
		}

	# Check OTP from Frappe DocType 'ConnectFest OTP' (check all active pending OTPs for this email)
	now_dt = frappe.utils.now_datetime()
	db_otps = frappe.db.sql("""
		SELECT name, otp, expires_at, status
		FROM `tabConnectFest OTP`
		WHERE email = %s AND status = 'Pending' AND expires_at >= %s
		ORDER BY creation DESC
	""", (target_email, now_dt), as_dict=True)

	is_valid = False
	matched_record_name = None

	if db_otps:
		for rec in db_otps:
			if str(rec.otp).strip() == submitted_otp:
				is_valid = True
				matched_record_name = rec.name
				break

	# Also check Redis cache
	cache = frappe.cache()
	cache_key = cache.make_key(f"{EMAIL_OTP_CACHE_PREFIX}{target_email}")
	stored_otp = cache.get(cache_key)
	if isinstance(stored_otp, bytes):
		stored_otp = stored_otp.decode("utf-8")

	if not is_valid and stored_otp and str(stored_otp).strip() == submitted_otp:
		is_valid = True

	if not is_valid:
		attempts = increment_failed_attempts(target_email)
		remaining = MAX_OTP_ATTEMPTS - attempts

		if remaining <= 0:
			duration_mins = trigger_block(target_email)
			frappe.local.response["http_status_code"] = 429
			return {
				"status": "error",
				"error_code": "ACCOUNT_TEMPORARILY_BLOCKED",
				"is_blocked": True,
				"message": f"Too many incorrect attempts. Your account is temporarily locked for {duration_mins} minutes.",
				"remaining_attempts": 0,
			}

		frappe.local.response["http_status_code"] = 400
		if remaining == 1:
			msg = "Invalid verification code. You have 1 attempt remaining before your account is temporarily locked."
		else:
			msg = f"Invalid verification code. You have {remaining} attempts remaining."

		return {
			"status": "error",
			"error_code": "INVALID_OTP",
			"message": msg,
			"remaining_attempts": remaining,
		}

	# Check for Type Mismatch between send_email_otp and verify_email_otp BEFORE consuming the OTP
	type_cache_key = cache.make_key(f"connectfest:otp_type:{target_email}")
	stored_send_type = cache.get(type_cache_key)
	if isinstance(stored_send_type, bytes):
		stored_send_type = stored_send_type.decode("utf-8")
	stored_send_type = str(stored_send_type or "").strip().lower()

	# Check whether user is registered for the specified type or in general
	reg_type = str(
		data.get("registration_type")
		or data.get("type")
		or data.get("role")
		or data.get("category")
		or kwargs.get("registration_type")
		or kwargs.get("type")
		or kwargs.get("role")
		or kwargs.get("category")
		or ""
	).strip().lower()

	# If verify did not pass type, inherit the type from send_email_otp
	if not reg_type and stored_send_type and stored_send_type != "any":
		reg_type = stored_send_type

	# Normalize verified registration type
	verified_type = "any"
	if reg_type in ("visitor", "vis", "visitor_registration"):
		verified_type = "visitor"
	elif reg_type in ("stall", "stalls", "exhibitor", "exhibitors", "stall_registration"):
		verified_type = "stall"
	elif reg_type in ("event", "events", "bni_event", "event_registration"):
		verified_type = "event"

	# Strict Mismatch Check: If send was for 'stall' and verify is for 'visitor' (or vice-versa)
	# DO NOT consume or expire the OTP so the user can immediately retry with the correct type!
	if stored_send_type and stored_send_type != "any" and verified_type != "any" and verified_type != stored_send_type:
		type_labels = {"stall": "Stall Booking", "visitor": "Visitor Pass", "event": "Event Registration"}
		sent_label = type_labels.get(stored_send_type, stored_send_type.title())
		verify_label = type_labels.get(verified_type, verified_type.title())
		frappe.local.response["http_status_code"] = 400
		return {
			"status": "error",
			"error_code": "TYPE_MISMATCH",
			"message": f"This verification code was requested for {sent_label}! You cannot verify it for {verify_label}. Please use the matching type or request a new OTP."
		}

	# Clear failed attempts on successful verification
	clear_failed_attempts(target_email)

	# Mark OTP record as Verified in Frappe DocType and expire any other pending OTPs for this email
	try:
		if matched_record_name:
			frappe.db.set_value("ConnectFest OTP", matched_record_name, {
				"status": "Verified",
				"verified_at": now_dt
			})
		frappe.db.sql("""
			UPDATE `tabConnectFest OTP`
			SET status = 'Expired'
			WHERE email = %s AND status = 'Pending'
		""", (target_email,))
		frappe.db.commit()
	except Exception:
		pass

	# Delete OTP from Redis cache after single use
	try:
		cache.delete(cache_key)
		cache.delete(type_cache_key)
	except Exception:
		pass

	# Mark email as verified in Redis cache with specific registration type
	verified_key = cache.make_key(f"{EMAIL_VERIFIED_CACHE_PREFIX}{target_email}")
	cache.setex(verified_key, get_email_verified_validity_seconds(), verified_type)

	is_reg_for_type = None
	if reg_type in ("visitor", "vis", "visitor_registration"):
		is_reg_for_type = bool(frappe.db.exists("ConnectFest Visitor Registration", {"email": target_email}))
	elif reg_type in ("stall", "stalls", "exhibitor", "exhibitors", "stall_registration"):
		is_reg_for_type = bool(frappe.db.exists("ConnectFest Registration", {"email": target_email}))
	elif reg_type in ("event", "events", "bni_event", "event_registration"):
		evt_name = data.get("event_name") or data.get("event") or kwargs.get("event_name")
		if evt_name:
			is_reg_for_type = bool(frappe.db.exists("ConnectFest BNI Event Registration", {"email": target_email, "event_name": evt_name}))
		else:
			is_reg_for_type = bool(frappe.db.exists("ConnectFest BNI Event Registration", {"email": target_email}))

	is_reg, reg_info = is_email_registered(target_email)

	# Fetch existing roles for attendee if any
	user_mob = (
		frappe.db.get_value("ConnectFest Registration", {"email": target_email}, "mobile_no")
		or frappe.db.get_value("ConnectFest Visitor Registration", {"email": target_email}, "mobile_no")
		or frappe.db.get_value("ConnectFest BNI Event Registration", {"email": target_email}, "mobile_no")
		or frappe.db.get_value("User", target_email, "mobile_no")
		or frappe.db.get_value("User", target_email, "phone")
		or ""
	)
	uid = (reg_info.get("user_id") if reg_info else None) or frappe.db.get_value("User", {"email": target_email}, "name") or target_email
	if uid and uid != "Administrator" and frappe.db.exists("User", uid):
		if not cint(frappe.db.get_value("User", uid, "enabled")):
			frappe.local.response["http_status_code"] = 403
			return {
				"status": "disabled",
				"is_verified": False,
				"message": "This account has been disabled by the Administrator. Access denied.",
			}

	from connectfest.services.auth import is_admin_user, get_user_permissions_summary
	perm_summary = get_user_permissions_summary(uid) if is_reg else get_user_permissions_summary(None)
	roles_arr = get_user_role_types(target_email, mobile_no=user_mob, user_id=uid) if is_reg else []
	is_scanner = bool(perm_summary["is_scanner"]) if is_reg else False

	is_admin = bool(is_admin_user(target_email) or is_admin_user(uid))
	if is_admin:
		is_reg = True
		admin_roles = perm_summary["roles"]
		for ar in admin_roles:
			if ar not in roles_arr:
				roles_arr.append(ar)
		if "Administrator" not in roles_arr:
			roles_arr.append("Administrator")

	# -------------------------------------------------------------
	# If NOT registered and NOT admin: Email verified only.
	# User must proceed to registration form before account/token is created!
	# -------------------------------------------------------------
	if not is_reg and not is_admin:
		frappe.local.response["http_status_code"] = 200
		return {
			"status": "success",
			"is_verified": True,
			"is_registered": False,
			"message": "Email address verified successfully! You may now proceed with registration.",
			"email": target_email,
		}

	# -------------------------------------------------------------
	# Log registered user / Admin into Frappe session (Email OTP Login)
	# -------------------------------------------------------------
	user_record_name = (
		frappe.db.get_value("ConnectFest Registration", {"email": target_email}, "full_name")
		or frappe.db.get_value("ConnectFest Visitor Registration", {"email": target_email}, "full_name")
		or frappe.db.get_value("ConnectFest BNI Event Registration", {"email": target_email}, "full_name")
		or frappe.db.get_value("User", uid, "full_name")
		or frappe.db.get_value("User", target_email, "full_name")
		or target_email.split("@")[0].title()
	)

	if not frappe.db.exists("User", uid):
		ensure_frappe_user(target_email, user_record_name, user_mob, role="Website User")

	from connectfest.services.auth import generate_auth_token
	role_label = (
		"Administrator" if is_admin
		else ("Exhibitor" if (roles_arr and "Exhibitor" in roles_arr)
		else ("sub_admin" if perm_summary["is_sub_admin"]
		else ("scanner" if is_scanner else "Attendee")))
	)
	login_identity = "Administrator" if is_admin else target_email
	auth_token = generate_auth_token(login_identity, role_type="admin" if is_admin else role_label)

	# Pure stateless X-Auth-Token (No browser session or cookie creation)
	frappe.local.response["http_status_code"] = 200
	return {
		"status": "success",
		"is_verified": True,
		"is_registered": True,
		"is_admin": is_admin,
		"is_super_admin": is_admin,
		"is_sub_admin": perm_summary["is_sub_admin"],
		"has_admin_access": perm_summary["has_admin_access"],
		"message": "Email verification successful! You are logged in.",
		"roles": roles_arr,
		"user_roles": perm_summary["roles"],
		"role_labels": perm_summary["role_labels"],
		"access": perm_summary["access"],
		"allowed_modules": perm_summary["allowed_modules"],
		"scanner_access": perm_summary["scanner_access"],
		"permissions": perm_summary["permissions"],
		"is_scanner": is_scanner,
		"x_auth_token": auth_token,
		"user": target_email,
		"email": target_email,
		"full_name": user_record_name,
		"home_page": "/admin" if (is_admin or perm_summary["has_admin_access"]) else "/me",
	}


def is_email_verified(email, expected_type=None):
	"""
	Checks whether the email has been verified via verify_email_otp.
	If expected_type is provided ('stall', 'visitor', 'event'), verifies the OTP was issued for that specific type.
	"""
	if not email:
		return False
	target_email = str(email).strip().lower()
	cache = frappe.cache()
	verified_key = cache.make_key(f"{EMAIL_VERIFIED_CACHE_PREFIX}{target_email}")
	cached_val = cache.get(verified_key)
	if cached_val:
		if isinstance(cached_val, bytes):
			cached_val = cached_val.decode("utf-8")
		stored_type = str(cached_val).strip().lower()
		if not expected_type or stored_type in ("1", "any", "all", expected_type.lower()):
			return True
		return False

	validity_secs = get_email_verified_validity_seconds()
	cutoff = frappe.utils.add_to_date(frappe.utils.now_datetime(), seconds=-validity_secs)
	db_verified = frappe.db.sql("""
		SELECT name FROM `tabConnectFest OTP`
		WHERE email = %s AND status = 'Verified' AND verified_at >= %s
		LIMIT 1
	""", (target_email, cutoff))
	return bool(db_verified)


def verify_otp_inline(email, submitted_otp):
	"""
	Validates submitted OTP for an email directly during registration if provided inline.
	Returns True if valid, False otherwise.
	Marks email as verified in cache and DocType on success.
	"""
	if not email or not submitted_otp:
		return False
	target_email = str(email).strip().lower()
	submitted_otp = str(submitted_otp).strip()

	cache = frappe.cache()
	cache_key = cache.make_key(f"{EMAIL_OTP_CACHE_PREFIX}{target_email}")
	stored_otp = cache.get(cache_key)
	if isinstance(stored_otp, bytes):
		stored_otp = stored_otp.decode("utf-8")

	validity_secs = get_email_verified_validity_seconds()

	if stored_otp and str(stored_otp).strip() == submitted_otp:
		cache.delete(cache_key)
		verified_key = cache.make_key(f"{EMAIL_VERIFIED_CACHE_PREFIX}{target_email}")
		cache.setex(verified_key, validity_secs, "1")
		rec = frappe.db.sql("""
			SELECT name FROM `tabConnectFest OTP`
			WHERE email = %s AND status = 'Pending' ORDER BY creation DESC LIMIT 1
		""", (target_email,), as_dict=True)
		if rec:
			frappe.db.set_value("ConnectFest OTP", rec[0]["name"], {
				"status": "Verified",
				"verified_at": frappe.utils.now_datetime()
			})
			frappe.db.commit()
		return True

	now_dt = frappe.utils.now_datetime()
	db_otps = frappe.db.sql("""
		SELECT name, otp, expires_at FROM `tabConnectFest OTP`
		WHERE email = %s AND status = 'Pending' AND expires_at >= %s ORDER BY creation DESC
	""", (target_email, now_dt), as_dict=True)

	for r in db_otps:
		if str(r.get("otp")).strip() == submitted_otp:
			frappe.db.set_value("ConnectFest OTP", r["name"], {
				"status": "Verified",
				"verified_at": now_dt
			})
			frappe.db.commit()
			try:
				cache.delete(cache_key)
			except Exception:
				pass
			verified_key = cache.make_key(f"{EMAIL_VERIFIED_CACHE_PREFIX}{target_email}")
			cache.setex(verified_key, validity_secs, "1")
			clear_failed_attempts(target_email)
			return True

	return False
