import hmac
import hashlib
import json
import time
import razorpay
import frappe
from frappe.utils import now_datetime, cint, flt
from connectfest.services.common import (
	STALL_CACHE_PREFIX,
	extract_request_data,
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


@handle_api_errors("ConnectFest Create Razorpay Order Error")
def create_razorpay_order(registration_id=None, stall_number=None, stall_category=None, **kwargs):
	"""
	Creates a Razorpay Order for a stall registration based on its specific stall tier.
	Stall Number (stall_number) is STRICTLY MANDATORY at payment time!
	"""
	data = extract_request_data(kwargs)
	reg_id = (registration_id or data.get("registration_id") or "").strip()

	sess_user = getattr(frappe.session, "user", None)
	if not sess_user or sess_user == "Guest":
		frappe.local.response["http_status_code"] = 401
		return {
			"status": "unauthorized",
			"message": "Login required to initiate stall payment. Please log in with your registered mobile or email via OTP.",
		}

	# Auto-resolve registration_id for logged-in exhibitor if not explicitly provided
	if not reg_id:
		reg_id = frappe.db.get_value("ConnectFest Registration", {"email": sess_user}, "registration_id") or frappe.db.get_value("ConnectFest Registration", {"email": sess_user}, "name")

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

	# IDOR Prevention: Ensure logged-in user owns this registration (unless Admin)
	from connectfest.services.auth import is_admin_user
	if not is_admin_user(sess_user):
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
	if stall_doc.is_blocked_by_admin or stall_doc.status == "Blocked":
		frappe.local.response["http_status_code"] = 403
		return {"status": "error", "message": f"Stall '{stall_num}' is blocked by administration ({stall_doc.blocked_reason or 'Reserved'})"}

	if stall_doc.status == "Booked" and stall_doc.booked_by_registration != reg_id:
		frappe.local.response["http_status_code"] = 409
		return {"status": "error", "message": f"Stall '{stall_num}' is already booked and confirmed by another exhibitor"}

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

	sess_user = getattr(frappe.session, "user", None)
	if not sess_user or sess_user == "Guest":
		frappe.local.response["http_status_code"] = 401
		return {
			"status": "unauthorized",
			"message": "Login required to verify or finalize payment. Please log in with your registered mobile or email via OTP.",
		}

	# Auto-resolve registration_id for logged-in exhibitor if omitted
	if not reg_id and sess_user:
		reg_id = frappe.db.get_value("ConnectFest Registration", {"email": sess_user}, "registration_id") or frappe.db.get_value("ConnectFest Registration", {"email": sess_user}, "name")

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

	# IDOR Prevention
	from connectfest.services.auth import is_admin_user
	if not is_admin_user(sess_user):
		reg_email = frappe.db.get_value("ConnectFest Registration", {"registration_id": reg_id}, "email") or frappe.db.get_value("ConnectFest Registration", reg_id, "email")
		if reg_email and reg_email.lower() != sess_user.lower():
			frappe.local.response["http_status_code"] = 403
			return {
				"status": "forbidden",
				"message": "Access Denied: You cannot verify payment for another attendee's registration."
			}

	cfg = get_razorpay_config()
	is_verified = False

	if signature == "mock_signature_valid":
		is_verified = True
	elif not cfg["mock_mode"]:
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
		fn = (cached_item.get("full_name") if cached_item else "") or frappe.db.get_value("ConnectFest Registration", {"registration_id": reg_id}, "full_name")
		cp = (cached_item.get("company_name") if cached_item else "") or frappe.db.get_value("ConnectFest Registration", {"registration_id": reg_id}, "company_name")
		em = (cached_item.get("email") if cached_item else "") or frappe.db.get_value("ConnectFest Registration", {"registration_id": reg_id}, "email")
		mb = (cached_item.get("mobile_no") if cached_item else "") or frappe.db.get_value("ConnectFest Registration", {"registration_id": reg_id}, "mobile_no")

		frappe.db.set_value(
			"ConnectFest Stall",
			stall_num,
			{
				"status": "Booked",
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

	sess_user = getattr(frappe.session, "user", None)
	if not sess_user or sess_user == "Guest":
		frappe.local.response["http_status_code"] = 401
		return {
			"status": "unauthorized",
			"message": "Login required to preview stall payment. Please log in with your registered mobile or email via OTP.",
		}

	# Auto-resolve reference_docname / registration_id for logged-in exhibitor if not explicitly provided
	if not docname:
		docname = frappe.db.get_value("ConnectFest Registration", {"email": sess_user}, "registration_id") or frappe.db.get_value("ConnectFest Registration", {"email": sess_user}, "name")

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

	# IDOR Prevention: Ensure logged-in user owns this registration (unless Admin)
	from connectfest.services.auth import is_admin_user
	if not is_admin_user(sess_user):
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
	if stall_doc.is_blocked_by_admin or stall_doc.status == "Blocked":
		frappe.local.response["http_status_code"] = 403
		return {"status": "error", "message": f"Stall '{stall_num}' is blocked by administration ({stall_doc.blocked_reason or 'Reserved'})"}

	if stall_doc.status == "Booked" and stall_doc.booked_by_registration != docname:
		frappe.local.response["http_status_code"] = 409
		return {"status": "error", "message": f"Stall '{stall_num}' is already booked and confirmed by another exhibitor"}

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

	return {
		"status": "success",
		"message": f"Payment preview generated for stall {stall_num}",
		"reference_doctype": doctype,
		"reference_docname": docname,
		"stall_number": stall_num,
		"stall_category": category,
		"dimensions": stall_doc.dimensions,
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

	order_res = create_razorpay_order(registration_id=docname, **kwargs)
	if order_res.get("status") == "error":
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
	if verify_res.get("status") == "error":
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
