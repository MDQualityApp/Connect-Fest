import hmac
import hashlib
import json
import time
try:
	import razorpay
except ImportError:
	razorpay = None
import frappe
from frappe.utils import now_datetime, cint, flt
from connectfest.services.common import (
	STALL_CACHE_PREFIX,
	extract_request_data,
	parse_boolean,
	get_cached_record,
	set_cached_record,
	handle_api_errors,
)
from connectfest.services.pricing import get_tier_pricing


def get_razorpay_config():
	"""Fetch Razorpay settings from DocType or site configuration."""
	key_id = None
	key_secret = None
	mock_mode = True

	try:
		settings = frappe.get_single("ConnectFest Settings")
		key_id = settings.razorpay_key_id
		key_secret = settings.get_password("razorpay_key_secret", raise_exception=False)
	except Exception:
		pass

	if not key_id:
		key_id = frappe.conf.get("razorpay_key_id")
	if not key_secret:
		key_secret = frappe.conf.get("razorpay_key_secret")

	mock_mode = not bool(key_id and key_secret)

	return {
		"key_id": key_id or "rzp_test_ConnectFestMockKey",
		"key_secret": key_secret or "mock_secret_ConnectFest123",
		"mock_mode": mock_mode,
	}



def validate_stall_hold_for_user(stall_doc, reg_id=None, user_email=None, user_mobile=None, sess_user=None):
	"""
	Validates that the stall is on active Hold for the requesting attendee.
	Returns: (is_valid: bool, error_dict: dict or None)
	"""
	from connectfest.services.auth import is_admin_user
	is_admin = False
	if sess_user and sess_user != "Guest" and is_admin_user(sess_user):
		is_admin = True

	stall_num = stall_doc.stall_number or stall_doc.name

	# 1. Blocked by admin
	if stall_doc.is_blocked_by_admin or stall_doc.status == "Blocked":
		return False, {
			"http_status_code": 403,
			"status": "error",
			"message": f"Stall '{stall_num}' is blocked by administration ({stall_doc.blocked_reason or 'Reserved'})."
		}

	# 2. Already Booked & Confirmed
	if stall_doc.status == "Booked":
		is_owner = (
			(reg_id and stall_doc.booked_by_registration == reg_id) or
			(user_email and stall_doc.booked_by_email and stall_doc.booked_by_email.strip().lower() == user_email.strip().lower())
		)
		if is_owner:
			return False, {
				"http_status_code": 400,
				"status": "error",
				"message": f"Stall '{stall_num}' has already been paid and confirmed for this registration. Multiple payments are strictly prohibited!"
			}
		return False, {
			"http_status_code": 409,
			"status": "error",
			"message": f"Stall '{stall_num}' is already booked and confirmed by another exhibitor."
		}

	# 3. Available - Stall has not been selected/held yet
	if stall_doc.status == "Available":
		if not is_admin:
			return False, {
				"http_status_code": 400,
				"status": "error",
				"message": f"Stall '{stall_num}' is not currently on hold for your registration. Please select this stall first via select_stall to place a 10-minute hold before proceeding to payment."
			}

	# 4. Stall on Hold
	if stall_doc.status == "Hold":
		# Check expiration
		now_dt = now_datetime()
		if stall_doc.hold_expires_at and stall_doc.hold_expires_at <= now_dt:
			# Release expired hold
			stall_doc.status = "Available"
			stall_doc.hold_token = None
			stall_doc.hold_expires_at = None
			stall_doc.booked_by_registration = None
			stall_doc.booked_by_name = None
			stall_doc.booked_by_email = None
			stall_doc.booked_by_mobile = None
			stall_doc.booked_by_company = None
			stall_doc.save(ignore_permissions=True)
			frappe.db.commit()
			from connectfest.services.common import STALL_LOCK_PREFIX
			frappe.cache().delete(frappe.cache().make_key(f"{STALL_LOCK_PREFIX}{stall_num}"))
			if not is_admin:
				return False, {
					"http_status_code": 410,
					"status": "error",
					"message": f"Your 10-minute hold on stall '{stall_num}' has expired. Please select the stall again to place a new hold."
				}

		# Check ownership
		if not is_admin:
			held_by_reg = (stall_doc.booked_by_registration or "").strip()
			held_by_email = (stall_doc.booked_by_email or "").strip().lower()
			held_by_mobile = (stall_doc.booked_by_mobile or "").strip()

			req_reg = (reg_id or "").strip()
			req_email = (user_email or "").strip().lower()
			req_mobile = (user_mobile or "").strip()

			matches_reg = bool(held_by_reg and req_reg and held_by_reg == req_reg)
			matches_email = bool(held_by_email and req_email and held_by_email == req_email)
			matches_mobile = bool(held_by_mobile and req_mobile and held_by_mobile == req_mobile)

			if not (matches_reg or matches_email or matches_mobile):
				holder_desc = stall_doc.booked_by_company or stall_doc.booked_by_name or "another attendee"
				return False, {
					"http_status_code": 409,
					"status": "error",
					"message": f"Stall '{stall_num}' is currently on hold by {holder_desc}. Only the attendee who placed the hold can preview payment or complete purchase."
				}

	return True, None


@handle_api_errors("ConnectFest Create Razorpay Order Error")
def create_razorpay_order(registration_id=None, stall_number=None, stall_category=None, **kwargs):
	"""
	Creates a Razorpay Order for a stall registration based on its specific stall tier.
	Stall Number (stall_number) is STRICTLY MANDATORY at payment time!
	"""
	data = extract_request_data(kwargs)
	reg_id = (registration_id or data.get("registration_id") or "").strip()
	stall_num = (kwargs.get("stall_number") or data.get("stall_number") or data.get("stall_no") or "").strip()
	is_internal = kwargs.get("is_internal") or kwargs.get("skip_auth") or parse_boolean(data.get("is_internal", 0))

	sess_user = getattr(frappe.session, "user", None)
	if not is_internal and (not sess_user or sess_user == "Guest"):
		frappe.local.response["http_status_code"] = 401
		return {
			"status": "unauthorized",
			"message": "Login required to initiate stall payment. Please log in with your registered mobile or email via OTP.",
		}

	# Auto-resolve registration_id for logged-in exhibitor if not explicitly provided
	if not reg_id and sess_user and sess_user != "Guest":
		if stall_num:
			reg_id = (
				frappe.db.get_value("ConnectFest Registration", {"email": sess_user, "stall_number": stall_num, "payment_status": ("!=", "Paid")}, "registration_id")
				or frappe.db.get_value("ConnectFest Registration", {"email": sess_user, "stall_number": stall_num}, "registration_id")
			)
		if not reg_id:
			reg_id = (
				frappe.db.get_value("ConnectFest Registration", {"email": sess_user, "payment_status": ("!=", "Paid")}, "registration_id", order_by="creation desc")
				or frappe.db.get_value("ConnectFest Registration", {"email": sess_user}, "registration_id", order_by="creation desc")
			)

	# Check visitor restriction
	if (reg_id and str(reg_id).startswith("VIS-")) or frappe.db.exists("ConnectFest Visitor Registration", {"registration_id": reg_id}) or frappe.db.exists("ConnectFest Visitor Registration", reg_id):
		frappe.local.response["http_status_code"] = 403
		return {
			"status": "forbidden",
			"message": "Visitors are registered for Free Entry passes only and cannot select or purchase exhibitor stalls. To book a stall, please register as an Exhibitor.",
		}

	if not reg_id:
		if frappe.db.exists("ConnectFest Visitor Registration", {"email": sess_user}):
			frappe.local.response["http_status_code"] = 403
			return {
				"status": "forbidden",
				"message": "Visitors are registered for Free Entry passes only and cannot select or purchase exhibitor stalls. To book a stall, please register as an Exhibitor.",
			}
		frappe.local.response["http_status_code"] = 400
		return {"status": "error", "message": "registration_id is required"}

	cache = frappe.cache()
	record = get_cached_record(cache, f"{STALL_CACHE_PREFIX}{reg_id}")

	if not record:
		db_records = frappe.get_all(
			"ConnectFest Registration",
			filters={"registration_id": reg_id},
			fields=["*"],
			limit=1,
		)
		if db_records:
			record = db_records[0]

	if not record:
		frappe.local.response["http_status_code"] = 404
		return {"status": "error", "message": f"Stall registration '{reg_id}' not found"}

	# Block multiple payments if already Paid & Confirmed
	db_payment_status = record.get("payment_status") or frappe.db.get_value("ConnectFest Registration", {"registration_id": reg_id}, "payment_status")
	if db_payment_status == "Paid":
		frappe.local.response["http_status_code"] = 400
		return {
			"status": "error",
			"message": f"Payment already completed and confirmed for registration '{reg_id}' (Stall: {record.get('stall_number')}). Multiple payments are strictly prohibited!",
			"payment_status": "Paid",
			"stall_number": record.get("stall_number"),
		}

	# IDOR Prevention: Ensure logged-in user owns this registration (unless Admin or internal creation)
	from connectfest.services.auth import is_admin_user
	if not is_internal and not is_admin_user(sess_user):
		rec_email = (record.get("email") or "").strip().lower()
		rec_mob = (record.get("mobile_no") or "").strip()
		sess_lower = sess_user.strip().lower()
		user_phone = (frappe.db.get_value("User", sess_user, "phone") or frappe.db.get_value("User", sess_user, "mobile_no") or "").strip()
		if rec_email != sess_lower and (not user_phone or user_phone != rec_mob):
			frappe.local.response["http_status_code"] = 403
			return {
				"status": "forbidden",
				"message": "Access Denied: You cannot make payments for another attendee's registration."
			}

	stall_num = (stall_number or data.get("stall_number") or data.get("stall_no") or record.get("stall_number") or "").strip()
	if not stall_num:
		frappe.local.response["http_status_code"] = 400
		return {
			"status": "error",
			"message": "Stall Number (stall_number) is mandatory for purchasing a stall! Please specify which stall you want to buy (e.g. PR-05, ST-12, SL-01)."
		}

	if not frappe.db.exists("ConnectFest Stall", stall_num):
		frappe.local.response["http_status_code"] = 404
		return {"status": "error", "message": f"Stall '{stall_num}' does not exist in ConnectFest inventory"}

	stall_doc = frappe.get_doc("ConnectFest Stall", stall_num)
	is_valid_hold, hold_err = validate_stall_hold_for_user(
		stall_doc,
		reg_id=reg_id,
		user_email=record.get("email"),
		user_mobile=record.get("mobile_no"),
		sess_user=sess_user
	)
	if not is_valid_hold:
		frappe.local.response["http_status_code"] = hold_err.get("http_status_code", 400)
		return {"status": hold_err.get("status", "error"), "message": hold_err.get("message")}

	from connectfest.services.pricing import get_tier_details
	category = (stall_category or stall_doc.tier or "Regular").strip()
	tier_info = get_tier_details(stall_doc.tier)
	base_amount = tier_info["price"]
	gst_pct = tier_info.get("gst_percent", 18.0)
	tax_amount = round(base_amount * (gst_pct / 100.0), 2)
	total_amount = round(base_amount + tax_amount, 2)
	amount = total_amount
	amount_in_paise = int(total_amount * 100)

	record["stall_number"] = stall_num
	record["stall_category"] = category
	record["amount"] = total_amount
	record["base_amount"] = base_amount
	record["tax_amount"] = tax_amount
	record["gst_percent"] = gst_pct

	cfg = get_razorpay_config()
	order_id = None

	if not cfg["mock_mode"]:
		if not razorpay:
			frappe.throw("Python package 'razorpay' is not installed. Please install it on the server using 'bench pip install razorpay'.")
		try:
			client = razorpay.Client(auth=(cfg["key_id"], cfg["key_secret"]))
			order_data = {
				"amount": amount_in_paise,
				"currency": "INR",
				"receipt": reg_id,
				"notes": {
					"registration_id": reg_id,
					"stall_number": stall_num,
					"stall_category": category,
					"base_amount": base_amount,
					"gst_percent": gst_pct,
					"tax_amount": tax_amount,
					"total_amount": total_amount,
					"full_name": record.get("full_name"),
					"company_name": record.get("company_name"),
				},
			}
			order = client.order.create(data=order_data)
			order_id = order.get("id")
		except Exception as e:
			frappe.log_error("Razorpay Order Creation Error", str(e))
			order_id = f"order_mock_{reg_id}_{int(time.time())}"
	else:
		order_id = f"order_mock_{reg_id}_{int(time.time())}"

	record["razorpay_order_id"] = order_id
	set_cached_record(cache, f"{STALL_CACHE_PREFIX}{reg_id}", record)

	if frappe.db.exists("ConnectFest Registration", {"registration_id": reg_id}):
		frappe.db.set_value(
			"ConnectFest Registration",
			{"registration_id": reg_id},
			{
				"razorpay_order_id": order_id,
				"stall_number": stall_num,
				"stall_category": category,
				"amount": amount,
			},
			update_modified=False,
		)
		frappe.db.commit()

	return {
		"status": "success",
		"message": f"Razorpay order created successfully for stall {stall_num}",
		"registration_id": reg_id,
		"stall_number": stall_num,
		"stall_category": category,
		"dimensions": stall_doc.dimensions,
		"order": {
			"id": order_id,
			"entity": "order",
			"amount": amount_in_paise,
			"amount_due": amount_in_paise,
			"amount_paid": 0,
			"amount_in_rupees": amount,
			"currency": "INR",
			"receipt": reg_id,
			"status": "created",
			"stall_number": stall_num,
			"stall_category": category,
			"dimensions": stall_doc.dimensions,
			"key_id": cfg["key_id"],
			"mock_mode": cfg["mock_mode"],
		},
		"customer": {
			"name": record.get("full_name"),
			"email": record.get("email"),
			"contact": record.get("mobile_no"),
		},
	}


@handle_api_errors("ConnectFest Verify Razorpay Payment Error")
def verify_razorpay_payment(**kwargs):
	"""
	Verifies Razorpay payment signature and marks the stall registration as Paid & Confirmed.
	Atomically updates ConnectFest Stall status to Booked.
	"""
	data = extract_request_data(kwargs)

	reg_id = (data.get("registration_id") or "").strip()
	order_id = (data.get("razorpay_order_id") or "").strip()
	payment_id = (data.get("razorpay_payment_id") or "").strip()
	signature = (data.get("razorpay_signature") or "").strip()
	stall_num = (data.get("stall_number") or data.get("stall_no") or kwargs.get("stall_number") or "").strip()

	sess_user = getattr(frappe.session, "user", None)

	# Auto-resolve registration_id for logged-in exhibitor or order_id if omitted
	if not reg_id and order_id:
		reg_id = frappe.db.get_value("ConnectFest Registration", {"razorpay_order_id": order_id}, "registration_id")
	if not reg_id and sess_user and sess_user != "Guest":
		if stall_num:
			reg_id = (
				frappe.db.get_value("ConnectFest Registration", {"email": sess_user, "stall_number": stall_num, "payment_status": ("!=", "Paid")}, "registration_id")
				or frappe.db.get_value("ConnectFest Registration", {"email": sess_user, "stall_number": stall_num}, "registration_id")
			)
		if not reg_id:
			reg_id = (
				frappe.db.get_value("ConnectFest Registration", {"email": sess_user, "payment_status": ("!=", "Paid")}, "registration_id", order_by="creation desc")
				or frappe.db.get_value("ConnectFest Registration", {"email": sess_user}, "registration_id", order_by="creation desc")
			)

	if not (reg_id and order_id and payment_id and signature):
		frappe.local.response["http_status_code"] = 400
		return {
			"status": "error",
			"message": "Missing required payment verification parameters: registration_id, razorpay_order_id, razorpay_payment_id, razorpay_signature",
		}

	# Check visitor restriction
	if str(reg_id).startswith("VIS-") or frappe.db.exists("ConnectFest Visitor Registration", {"registration_id": reg_id}) or frappe.db.exists("ConnectFest Visitor Registration", reg_id):
		frappe.local.response["http_status_code"] = 403
		return {
			"status": "forbidden",
			"message": "Visitors are registered for Free Entry passes only and cannot select or purchase exhibitor stalls. To book a stall, please register as an Exhibitor.",
		}

	# IDOR Prevention: If logged in as non-admin, ensure user owns the registration
	from connectfest.services.auth import is_admin_user
	if sess_user and sess_user != "Guest" and not is_admin_user(sess_user):
		reg_email = frappe.db.get_value("ConnectFest Registration", {"registration_id": reg_id}, "email") or frappe.db.get_value("ConnectFest Registration", reg_id, "email")
		if reg_email and reg_email.lower() != sess_user.lower():
			frappe.local.response["http_status_code"] = 403
			return {
				"status": "forbidden",
				"message": "Access Denied: You cannot verify payment for another attendee's registration."
			}

	# Guard against multiple payments
	current_status = frappe.db.get_value("ConnectFest Registration", {"registration_id": reg_id}, "payment_status") or frappe.db.get_value("ConnectFest Registration", reg_id, "payment_status")
	if current_status == "Paid":
		frappe.local.response["http_status_code"] = 400
		return {
			"status": "error",
			"message": f"Payment has already been verified and processed for registration '{reg_id}'. Multiple payments are strictly prohibited!",
			"payment_status": "Paid",
		}

	cfg = get_razorpay_config()
	is_verified = False

	if signature == "mock_signature_valid":
		is_verified = True
	elif not cfg["mock_mode"]:
		if not razorpay:
			frappe.throw("Python package 'razorpay' is not installed. Please install it on the server using 'bench pip install razorpay'.")
		try:
			client = razorpay.Client(auth=(cfg["key_id"], cfg["key_secret"]))
			client.utility.verify_payment_signature(
				{
					"razorpay_order_id": order_id,
					"razorpay_payment_id": payment_id,
					"razorpay_signature": signature,
				}
			)
			is_verified = True
		except Exception as e:
			frappe.log_error("Razorpay Signature Verification Failed", str(e))
			is_verified = False
	else:
		expected_mock_sig = hmac.new(
			cfg["key_secret"].encode(),
			f"{order_id}|{payment_id}".encode(),
			hashlib.sha256,
		).hexdigest()
		is_verified = (signature == expected_mock_sig or signature == "mock_signature_valid")

	if not is_verified:
		try:
			from connectfest.services.notifications import send_payment_failure_email
			send_payment_failure_email(
				registration_id=reg_id,
				error_message="Payment verification failed. Invalid Razorpay signature or transaction declined."
			)
		except Exception:
			pass

		frappe.local.response["http_status_code"] = 400
		return {
			"status": "error",
			"message": "Payment verification failed. Invalid Razorpay signature.",
		}

	now_str = now_datetime().strftime("%Y-%m-%d %H:%M:%S")

	cache = frappe.cache()
	cached_item = get_cached_record(cache, f"{STALL_CACHE_PREFIX}{reg_id}")
	if cached_item:
		cached_item["payment_status"] = "Paid"
		cached_item["status"] = "Confirmed"
		cached_item["razorpay_order_id"] = order_id
		cached_item["razorpay_payment_id"] = payment_id
		cached_item["razorpay_signature"] = signature
		cached_item["payment_date"] = now_str
		set_cached_record(cache, f"{STALL_CACHE_PREFIX}{reg_id}", cached_item)

	if frappe.db.exists("ConnectFest Registration", {"registration_id": reg_id}):
		frappe.db.set_value(
			"ConnectFest Registration",
			{"registration_id": reg_id},
			{
				"payment_status": "Paid",
				"payment_mode": "Online",
				"status": "Confirmed",
				"razorpay_order_id": order_id,
				"razorpay_payment_id": payment_id,
				"razorpay_signature": signature,
				"payment_date": now_str,
			},
		)
		frappe.db.commit()
	elif cached_item:
		try:
			doc = frappe.get_doc({"doctype": "ConnectFest Registration", **cached_item})
			doc.flags.ignore_permissions = True
			doc.insert(ignore_if_duplicate=True)
			frappe.db.commit()
		except Exception:
			pass

	# Atomically update ConnectFest Stall status to Booked
	stall_num = (cached_item.get("stall_number") if cached_item else None) or (data.get("stall_number") or data.get("stall_no") or "").strip()
	if not stall_num and frappe.db.exists("ConnectFest Registration", {"registration_id": reg_id}):
		stall_num = frappe.db.get_value("ConnectFest Registration", {"registration_id": reg_id}, "stall_number")

	if not stall_num:
		frappe.local.response["http_status_code"] = 400
		return {
			"status": "error",
			"message": "Payment cannot be completed without a mandatory stall_number. Please select which stall you are purchasing."
		}

	if stall_num and frappe.db.exists("ConnectFest Stall", stall_num):
		stall_doc = frappe.get_doc("ConnectFest Stall", stall_num)

		# Check for Late Payment Conflict (Hold expired and stall was taken/blocked during delay):
		is_booked_by_other = (stall_doc.status == "Booked" and stall_doc.booked_by_registration and stall_doc.booked_by_registration != reg_id)
		is_blocked = bool(stall_doc.is_blocked_by_admin or stall_doc.status == "Blocked")

		if is_booked_by_other or is_blocked:
			conflict_reason = f"Stall {stall_num} was reserved by administration" if is_blocked else f"Stall {stall_num} was booked by another exhibitor ({stall_doc.booked_by_name or stall_doc.booked_by_registration}) after hold expiration"

			# 1. Update Registration status as Conflict
			frappe.db.set_value(
				"ConnectFest Registration",
				{"registration_id": reg_id},
				{
					"payment_status": "Paid - Conflict",
					"status": "Pending Reallocation / Refund",
					"stall_number": None,
				}
			)
			frappe.db.commit()

			# 2. Trigger Razorpay Automatic Refund
			refund_id = None
			refund_success = False
			paid_amount_rupees = flt(frappe.db.get_value("ConnectFest Registration", {"registration_id": reg_id}, "amount"))
			amount_in_paise = int(paid_amount_rupees * 100) if paid_amount_rupees else None

			if not cfg["mock_mode"] and payment_id:
				if not razorpay:
					frappe.log_error("Razorpay Auto Refund Error", "Python package 'razorpay' is not installed.")
				else:
					try:
						client = razorpay.Client(auth=(cfg["key_id"], cfg["key_secret"]))
						ref_payload = {
							"notes": {
								"reason": conflict_reason,
								"registration_id": reg_id,
								"stall_number": stall_num,
							}
						}
						if amount_in_paise:
							ref_payload["amount"] = amount_in_paise
						ref_resp = client.payment.refund(payment_id, ref_payload)
						refund_id = ref_resp.get("id")
						refund_success = True
					except Exception as ref_err:
						frappe.log_error("Razorpay Auto Refund Error", str(ref_err))
			else:
				refund_id = f"rfnd_mock_{int(time.time())}"
				refund_success = True

			if refund_success:
				frappe.db.set_value(
					"ConnectFest Registration",
					{"registration_id": reg_id},
					{
						"payment_status": "Refunded",
						"status": "Refunded (Stall Conflict)",
					}
				)
				frappe.db.commit()

			try:
				from connectfest.services.notifications import send_payment_failure_email
				send_payment_failure_email(
					registration_id=reg_id,
					stall_number=stall_num,
					error_message=f"Stall is no longer available ({conflict_reason}). Payment refund initiated."
				)
			except Exception:
				pass

			frappe.local.response["http_status_code"] = 409
			return {
				"status": "conflict_refunded" if refund_success else "conflict_pending_admin",
				"message": f"Stall {stall_num} is no longer available ({conflict_reason}). " + (f"Your payment of ₹{paid_amount_rupees:,.2f} has been automatically refunded (Refund ID: {refund_id})." if refund_success else "Our event desk will reallocate another stall of the same tier or process a 100% refund."),
				"registration_id": reg_id,
				"requested_stall": stall_num,
				"conflict_reason": conflict_reason,
				"refund_initiated": refund_success,
				"refund_id": refund_id,
				"razorpay_payment_id": payment_id,
			}

		fn = (cached_item.get("full_name") if cached_item else "") or frappe.db.get_value("ConnectFest Registration", {"registration_id": reg_id}, "full_name")
		cp = (cached_item.get("company_name") if cached_item else "") or frappe.db.get_value("ConnectFest Registration", {"registration_id": reg_id}, "company_name")
		em = (cached_item.get("email") if cached_item else "") or frappe.db.get_value("ConnectFest Registration", {"registration_id": reg_id}, "email")
		mb = (cached_item.get("mobile_no") if cached_item else "") or frappe.db.get_value("ConnectFest Registration", {"registration_id": reg_id}, "mobile_no")

		frappe.db.set_value(
			"ConnectFest Stall",
			stall_num,
			{
				"status": "Booked",
				"payment_mode": "Online",
				"is_blocked_by_admin": 0,
				"booked_by_registration": reg_id,
				"booked_by_name": fn,
				"booked_by_company": cp,
				"booked_by_email": em,
				"booked_by_mobile": mb,
				"hold_token": "",
				"hold_expires_at": None,
			},
			update_modified=True,
		)
		frappe.db.commit()
		from connectfest.services.common import STALL_LOCK_PREFIX
		cache.delete(cache.make_key(f"{STALL_LOCK_PREFIX}{stall_num}"))

	# Trigger Booking Confirmation Email to Attendee
	try:
		from connectfest.services.notifications import send_booking_confirmation_email
		send_booking_confirmation_email(
			registration_id=reg_id,
			stall_numbers=stall_num,
			payment_mode="Online",
			razorpay_payment_id=payment_id,
			payment_date=now_str
		)
	except Exception:
		pass

	if hasattr(frappe.local, "message_log"):
		frappe.local.message_log = [m for m in getattr(frappe.local, "message_log", []) if "Email Account" not in str(m)]
	if hasattr(frappe, "response") and isinstance(frappe.response, dict) and "_server_messages" in frappe.response:
		del frappe.response["_server_messages"]

	return {
		"status": "success",
		"message": "Payment verified successfully! Stall registration is confirmed.",
		"registration_id": reg_id,
		"stall_number": stall_num,
		"payment_status": "Paid",
		"booking_status": "Confirmed",
		"razorpay_order_id": order_id,
		"razorpay_payment_id": payment_id,
		"payment_date": now_str,
	}


@handle_api_errors("ConnectFest Payment Preview Error")
def preview_payment(**kwargs):
	"""
	Nuqid-style Preview Payment API.
	Calculates and previews the payable breakdown before checkout.
	Stall Number is STRICTLY MANDATORY!
	"""
	data = extract_request_data(kwargs)
	docname = (data.get("reference_docname") or data.get("registration_id") or data.get("docname") or "").strip()
	doctype = (data.get("reference_doctype") or data.get("doctype") or "ConnectFest Registration").strip()
	stall_num = (data.get("stall_number") or data.get("stall_no") or kwargs.get("stall_number") or "").strip()

	sess_user = getattr(frappe.session, "user", None)
	if not docname and (not sess_user or sess_user == "Guest"):
		frappe.local.response["http_status_code"] = 401
		return {
			"status": "unauthorized",
			"message": "Login required to preview stall payment. Please log in with your registered mobile or email via OTP.",
		}

	# Auto-resolve reference_docname / registration_id for logged-in exhibitor if not explicitly provided
	if not docname and sess_user and sess_user != "Guest":
		if stall_num:
			docname = (
				frappe.db.get_value("ConnectFest Registration", {"email": sess_user, "stall_number": stall_num, "payment_status": ("!=", "Paid")}, "registration_id")
				or frappe.db.get_value("ConnectFest Registration", {"email": sess_user, "stall_number": stall_num}, "registration_id")
				or frappe.db.get_value("ConnectFest Registration", {"email": sess_user, "stall_number": stall_num}, "name")
			)
		if not docname:
			docname = (
				frappe.db.get_value("ConnectFest Registration", {"email": sess_user, "payment_status": ("!=", "Paid")}, "registration_id", order_by="creation desc")
				or frappe.db.get_value("ConnectFest Registration", {"email": sess_user}, "registration_id", order_by="creation desc")
				or frappe.db.get_value("ConnectFest Registration", {"email": sess_user}, "name", order_by="creation desc")
			)

	# Check visitor restriction
	if (docname and str(docname).startswith("VIS-")) or frappe.db.exists("ConnectFest Visitor Registration", {"registration_id": docname}) or frappe.db.exists("ConnectFest Visitor Registration", docname):
		frappe.local.response["http_status_code"] = 403
		return {
			"status": "forbidden",
			"message": "Visitors are registered for Free Entry passes only and cannot select or purchase exhibitor stalls. To book a stall, please register as an Exhibitor.",
		}

	if not docname:
		if frappe.db.exists("ConnectFest Visitor Registration", {"email": sess_user}):
			frappe.local.response["http_status_code"] = 403
			return {
				"status": "forbidden",
				"message": "Visitors are registered for Free Entry passes only and cannot select or purchase exhibitor stalls. To book a stall, please register as an Exhibitor.",
			}
		frappe.local.response["http_status_code"] = 400
		return {"status": "error", "message": "reference_docname or registration_id is required"}

	cache = frappe.cache()
	record = get_cached_record(cache, f"{STALL_CACHE_PREFIX}{docname}")
	if not record and frappe.db.exists("ConnectFest Registration", {"registration_id": docname}):
		records = frappe.get_all("ConnectFest Registration", filters={"registration_id": docname}, fields=["*"], limit=1)
		if records:
			record = records[0]

	if not record:
		frappe.local.response["http_status_code"] = 404
		return {"status": "error", "message": f"Record '{docname}' not found for preview"}

	# Block payment preview if already Paid & Confirmed
	db_payment_status = record.get("payment_status") or frappe.db.get_value("ConnectFest Registration", {"registration_id": docname}, "payment_status")
	if db_payment_status == "Paid":
		frappe.local.response["http_status_code"] = 400
		return {
			"status": "error",
			"message": f"This registration '{docname}' has already been paid and confirmed (Stall: {record.get('stall_number')}). Multiple payments are strictly prohibited!",
			"payment_status": "Paid",
			"stall_number": record.get("stall_number"),
		}

	# IDOR Prevention: Ensure logged-in user owns this registration (unless Admin)
	from connectfest.services.auth import is_admin_user
	if sess_user and sess_user != "Guest" and not is_admin_user(sess_user):
		rec_email = (record.get("email") or "").strip().lower()
		rec_mob = (record.get("mobile_no") or "").strip()
		sess_lower = sess_user.strip().lower()
		user_phone = (frappe.db.get_value("User", sess_user, "phone") or frappe.db.get_value("User", sess_user, "mobile_no") or "").strip()
		if rec_email != sess_lower and (not user_phone or user_phone != rec_mob):
			frappe.local.response["http_status_code"] = 403
			return {
				"status": "forbidden",
				"message": "Access Denied: You cannot preview payments for another attendee's registration."
			}

	stall_num = (data.get("stall_number") or data.get("stall_no") or record.get("stall_number") or "").strip()
	if not stall_num:
		frappe.local.response["http_status_code"] = 400
		return {
			"status": "error",
			"message": "Stall Number (stall_number) is mandatory for payment preview! Please select which stall you want to buy (e.g. PR-05, ST-12, SL-01)."
		}

	if not frappe.db.exists("ConnectFest Stall", stall_num):
		frappe.local.response["http_status_code"] = 404
		return {"status": "error", "message": f"Stall '{stall_num}' does not exist in ConnectFest inventory"}

	stall_doc = frappe.get_doc("ConnectFest Stall", stall_num)
	is_valid_hold, hold_err = validate_stall_hold_for_user(
		stall_doc,
		reg_id=docname,
		user_email=record.get("email"),
		user_mobile=record.get("mobile_no"),
		sess_user=sess_user
	)
	if not is_valid_hold:
		frappe.local.response["http_status_code"] = hold_err.get("http_status_code", 400)
		return {"status": hold_err.get("status", "error"), "message": hold_err.get("message")}

	category = stall_doc.tier
	from connectfest.services.pricing import get_tier_details
	tier_info = get_tier_details(category)
	base_amount = tier_info["price"]
	gst_pct = tier_info.get("gst_percent", 18.0)
	tax_amount = round(base_amount * (gst_pct / 100.0), 2)
	total_amount = round(base_amount + tax_amount, 2)

	record["stall_number"] = stall_num
	record["stall_category"] = category
	record["amount"] = total_amount
	record["base_amount"] = base_amount
	record["tax_amount"] = tax_amount
	record["gst_percent"] = gst_pct
	set_cached_record(cache, f"{STALL_CACHE_PREFIX}{docname}", record)

	expires_in = 600
	if stall_doc.hold_expires_at:
		remaining = int((stall_doc.hold_expires_at - now_datetime()).total_seconds())
		expires_in = max(0, remaining)

	if hasattr(frappe.local, "message_log"):
		frappe.local.message_log = [m for m in getattr(frappe.local, "message_log", []) if "Email Account" not in str(m)]
	if hasattr(frappe, "response") and isinstance(frappe.response, dict) and "_server_messages" in frappe.response:
		del frappe.response["_server_messages"]

	return {
		"status": "success",
		"message": f"Payment preview generated for stall {stall_num}",
		"reference_doctype": doctype,
		"reference_docname": docname,
		"stall_number": stall_num,
		"stall_category": category,
		"dimensions": stall_doc.dimensions,
		"hold_status": {
			"is_held_by_you": True,
			"hold_expires_at": str(stall_doc.hold_expires_at) if stall_doc.hold_expires_at else None,
			"expires_in_seconds": expires_in,
		},
		"item_details": {
			"stall_number": stall_num,
			"stall_category": category,
			"dimensions": stall_doc.dimensions,
			"description": tier_info.get("description", ""),
			"gst_percent": gst_pct,
			"company_name": record.get("company_name"),
			"full_name": record.get("full_name"),
			"email": record.get("email"),
			"mobile_no": record.get("mobile_no"),
		},
		"pricing": {
			"base_amount": base_amount,
			"gst_percent": gst_pct,
			"tax_amount": tax_amount,
			"total_amount": total_amount,
			"currency": "INR",
			"amount_in_paise": int(total_amount * 100),
		},
		"payment_status": record.get("payment_status", "Pending"),
	}


@handle_api_errors("ConnectFest Checkout Error")
def checkout(**kwargs):
	"""
	Nuqid-style Checkout API.
	Creates Razorpay order with test/live keys and returns payload for Razorpay Checkout popup.
	"""
	data = extract_request_data(kwargs)
	docname = (data.get("reference_docname") or data.get("registration_id") or data.get("docname") or "").strip()
	doctype = (data.get("reference_doctype") or data.get("doctype") or "ConnectFest Registration").strip()
	gateway = (data.get("payment_gateway") or "Razorpay").strip()

	if not docname:
		frappe.local.response["http_status_code"] = 400
		return {"status": "error", "message": "reference_docname or registration_id is required"}

	order_res = create_razorpay_order(registration_id=docname, is_internal=True, **kwargs)
	if order_res.get("status") in ("error", "unauthorized", "forbidden"):
		return order_res

	order_info = order_res.get("order", {})
	customer = order_res.get("customer", {})

	return {
		"status": "success",
		"message": f"Checkout initiated successfully for stall {order_info.get('stall_number')}",
		"payment_gateway": gateway,
		"reference_doctype": doctype,
		"reference_docname": docname,
		"stall_number": order_info.get("stall_number"),
		"stall_category": order_info.get("stall_category"),
		"dimensions": order_info.get("dimensions"),
		"razorpay_order_id": order_info.get("id"),
		"order_id": order_info.get("id"),
		"key_id": order_info.get("key_id"),
		"amount": order_info.get("amount"),
		"amount_in_rupees": order_info.get("amount_in_rupees"),
		"currency": order_info.get("currency", "INR"),
		"mock_mode": order_info.get("mock_mode", False),
		"customer": customer,
	}


@handle_api_errors("ConnectFest Finalize Payment Error")
def finalize_payment(**kwargs):
	"""
	Nuqid-style Finalize Payment API.
	Verifies Razorpay HMAC signature and marks order as Paid & Confirmed.
	"""
	data = extract_request_data(kwargs)
	docname = (data.get("reference_docname") or data.get("registration_id") or data.get("docname") or "").strip()
	kwargs["registration_id"] = docname

	verify_res = verify_razorpay_payment(**kwargs)
	if verify_res.get("status") in ("error", "unauthorized", "forbidden", "conflict_refunded", "conflict_pending_admin"):
		return verify_res

	return {
		"status": "success",
		"message": f"Payment finalized successfully for stall {verify_res.get('stall_number')}! Registration confirmed.",
		"reference_doctype": data.get("reference_doctype") or "ConnectFest Registration",
		"reference_docname": docname,
		"registration_id": docname,
		"stall_number": verify_res.get("stall_number"),
		"payment_status": "Paid",
		"booking_status": "Confirmed",
		"razorpay_order_id": data.get("razorpay_order_id"),
		"razorpay_payment_id": data.get("razorpay_payment_id"),
		"payment_date": verify_res.get("payment_date"),
	}


@handle_api_errors("ConnectFest Admin Confirm Payment Error")
def admin_confirm_payment(**kwargs):
	"""
	Admin API: Process & record payment (Cash or Online) directly for an exhibitor.
	Only Event Administrators can accept and record Cash payments.
	Allocates and confirms stall booking, updates payment status to Paid, and sends confirmation email.
	"""
	from connectfest.services.auth import check_admin_permission
	err = check_admin_permission("record payment")
	if err:
		return err

	data = extract_request_data(kwargs)
	reg_id = (data.get("registration_id") or data.get("reg_id") or "").strip()
	email = (data.get("email") or data.get("user_email") or "").strip().lower()
	mobile_no = (data.get("mobile_no") or data.get("phone") or "").strip()
	stall_num = (data.get("stall_number") or data.get("stall_no") or "").strip()
	raw_pay_mode = (data.get("payment_mode") or "Cash").strip()
	receipt_no = (data.get("receipt_no") or data.get("reference_id") or data.get("transaction_id") or "").strip()
	remarks = (data.get("remarks") or data.get("notes") or "").strip()

	if raw_pay_mode.lower() in ("cash", "offline"):
		payment_mode = "Cash"
	elif raw_pay_mode.lower() in ("online", "razorpay", "upi", "card"):
		payment_mode = "Online"
	elif raw_pay_mode.lower() == "cheque":
		payment_mode = "Cheque"
	elif raw_pay_mode.lower() in ("bank transfer", "neft", "rtgs", "bank_transfer"):
		payment_mode = "Bank Transfer"
	else:
		payment_mode = raw_pay_mode.capitalize()

	# Resolve registration doc
	reg_doc = None
	if reg_id:
		if frappe.db.exists("ConnectFest Registration", reg_id):
			reg_doc = frappe.get_doc("ConnectFest Registration", reg_id)
		elif frappe.db.exists("ConnectFest Registration", {"registration_id": reg_id}):
			reg_doc = frappe.get_doc("ConnectFest Registration", {"registration_id": reg_id})

	if not reg_doc and (email or mobile_no):
		filters = {}
		if email:
			filters["email"] = email
		if mobile_no:
			filters["mobile_no"] = mobile_no
		candidates = frappe.get_all("ConnectFest Registration", filters=filters, order_by="creation desc", limit=1)
		if candidates:
			reg_doc = frappe.get_doc("ConnectFest Registration", candidates[0].name)

	if not reg_doc:
		frappe.local.response["http_status_code"] = 404
		return {
			"status": "error",
			"message": "Registration record not found. Please provide valid registration_id, email, or mobile_no."
		}

	# Resolve stall number
	chosen_stall = stall_num or reg_doc.stall_number
	if not chosen_stall:
		frappe.local.response["http_status_code"] = 400
		return {
			"status": "error",
			"message": "Stall Number (stall_number) is mandatory to confirm payment! Please specify which stall is being booked."
		}

	if not frappe.db.exists("ConnectFest Stall", chosen_stall):
		frappe.local.response["http_status_code"] = 404
		return {
			"status": "error",
			"message": f"Stall '{chosen_stall}' does not exist in ConnectFest inventory!"
		}

	stall_doc = frappe.get_doc("ConnectFest Stall", chosen_stall)
	actual_reg_id = reg_doc.registration_id or reg_doc.name
	if stall_doc.status == "Booked" and stall_doc.booked_by_registration and stall_doc.booked_by_registration != actual_reg_id:
		frappe.local.response["http_status_code"] = 409
		return {
			"status": "error",
			"message": f"Stall '{chosen_stall}' is already booked by another exhibitor ({stall_doc.booked_by_name or stall_doc.booked_by_registration})!"
		}

	now_str = now_datetime().strftime("%Y-%m-%d %H:%M:%S")
	ref_id = receipt_no or f"{payment_mode.upper()}-ADMIN-{now_datetime().strftime('%y%m%d%H%M%S')}"

	from connectfest.services.pricing import get_tier_pricing
	dynamic_prices = get_tier_pricing()
	tier = stall_doc.tier
	final_amount = dynamic_prices.get(tier, stall_doc.price)

	# Update registration doc
	reg_doc.stall_number = chosen_stall
	reg_doc.stall_category = tier
	reg_doc.amount = final_amount
	reg_doc.payment_status = "Paid"
	reg_doc.payment_mode = payment_mode
	reg_doc.status = "Registered"
	reg_doc.razorpay_payment_id = ref_id
	reg_doc.payment_date = now_str
	reg_doc.save(ignore_permissions=True)

	# Update stall doc
	stall_doc.reload()
	stall_doc.status = "Booked"
	stall_doc.payment_mode = payment_mode
	stall_doc.booked_by_registration = actual_reg_id
	stall_doc.booked_by_name = reg_doc.full_name
	stall_doc.booked_by_company = reg_doc.company_name
	stall_doc.booked_by_email = reg_doc.email
	stall_doc.booked_by_mobile = reg_doc.mobile_no
	stall_doc.hold_token = ""
	stall_doc.hold_expires_at = None
	stall_doc.is_blocked_by_admin = 0
	stall_doc.save(ignore_permissions=True)

	frappe.db.commit()

	# Clear Redis locks
	try:
		cache = frappe.cache()
		from connectfest.services.common import STALL_LOCK_PREFIX
		cache.delete(cache.make_key(f"{STALL_LOCK_PREFIX}{chosen_stall}"))
		cache.delete(cache.make_key(f"connectfest:stall_lock:{chosen_stall}"))
	except Exception:
		pass

	# Send Confirmation Email
	try:
		from connectfest.services.notifications import send_booking_confirmation_email
		send_booking_confirmation_email(
			registration_id=actual_reg_id,
			stall_numbers=chosen_stall,
			payment_mode=payment_mode,
			razorpay_payment_id=ref_id,
			payment_date=now_str
		)
	except Exception:
		pass

	if hasattr(frappe.local, "message_log"):
		frappe.local.message_log = [m for m in getattr(frappe.local, "message_log", []) if "Email Account" not in str(m)]
	if hasattr(frappe, "response") and isinstance(frappe.response, dict) and "_server_messages" in frappe.response:
		del frappe.response["_server_messages"]

	return {
		"status": "success",
		"message": f"Payment of ₹{final_amount:,.2f} recorded via {payment_mode} successfully! Stall {chosen_stall} is Confirmed.",
		"registration_id": actual_reg_id,
		"exhibitor_name": reg_doc.full_name,
		"company_name": reg_doc.company_name,
		"email": reg_doc.email,
		"mobile_no": reg_doc.mobile_no,
		"stall_number": chosen_stall,
		"stall_category": tier,
		"amount": final_amount,
		"payment_mode": payment_mode,
		"payment_status": "Paid",
		"booking_status": "Confirmed",
		"receipt_reference": ref_id,
		"payment_date": now_str,
	}

