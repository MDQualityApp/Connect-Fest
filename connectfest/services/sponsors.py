import json
import uuid
import frappe
from frappe.utils import cint, now_datetime, validate_email_address
from connectfest.services.common import (
	extract_request_data,
	parse_boolean,
	handle_api_errors,
	save_image_from_base64_or_url,
	format_full_url,
)
from connectfest.services.auth import check_admin_permission


def _extract_id_list(raw_val):
	"""Extracts a list of clean string IDs from a list, JSON string, or comma-separated string."""
	if not raw_val:
		return []
	if isinstance(raw_val, list):
		return [str(item).strip() for item in raw_val if str(item).strip()]
	if isinstance(raw_val, str):
		val = raw_val.strip()
		if val.startswith("[") and val.endswith("]"):
			try:
				parsed = json.loads(val)
				if isinstance(parsed, list):
					return [str(item).strip() for item in parsed if str(item).strip()]
			except Exception:
				pass
			val = val.strip("[]")
		return [s.strip() for s in val.split(",") if s.strip()]
	return [str(raw_val).strip()]


def _format_sponsor_doc(doc_or_dict):
	"""Standardizes sponsor output dictionary for API and frontend JSON consumption."""
	if isinstance(doc_or_dict, dict):
		name = doc_or_dict.get("name")
		sponsor_name = doc_or_dict.get("sponsor_name") or ""
		sponsor_category = doc_or_dict.get("sponsor_category") or "Associate Sponsor"
		sponsored_for = doc_or_dict.get("sponsored_for") or ""
		logo = doc_or_dict.get("logo") or ""
		website_url = doc_or_dict.get("website_url") or ""
		stall_number = doc_or_dict.get("stall_number") or ""
		is_active = cint(doc_or_dict.get("is_active", 1))
		is_featured = cint(doc_or_dict.get("is_featured", 0))
		display_order = cint(doc_or_dict.get("display_order", 0))
		description = doc_or_dict.get("description") or ""
		contact_person = doc_or_dict.get("contact_person") or ""
		email = doc_or_dict.get("email") or ""
		phone = doc_or_dict.get("phone") or ""
		creation = str(doc_or_dict.get("creation") or "")
		modified = str(doc_or_dict.get("modified") or "")
	else:
		name = doc_or_dict.name
		sponsor_name = doc_or_dict.sponsor_name or ""
		sponsor_category = doc_or_dict.sponsor_category or "Associate Sponsor"
		sponsored_for = doc_or_dict.sponsored_for or ""
		logo = doc_or_dict.logo or ""
		website_url = doc_or_dict.website_url or ""
		stall_number = doc_or_dict.stall_number or ""
		is_active = cint(doc_or_dict.is_active)
		is_featured = cint(doc_or_dict.is_featured)
		display_order = cint(doc_or_dict.display_order)
		description = doc_or_dict.get("description") or ""
		contact_person = doc_or_dict.contact_person or ""
		email = doc_or_dict.email or ""
		phone = doc_or_dict.phone or ""
		creation = str(doc_or_dict.creation)
		modified = str(doc_or_dict.modified)

	return {
		"id": name,
		"name": name,
		"sponsor_name": sponsor_name,
		"sponsor_category": sponsor_category,
		"sponsored_for": sponsored_for,
		"logo": format_full_url(logo),
		"website_url": website_url,
		"stall_number": stall_number or None,
		"description": description,
		"display_order": display_order,
		"is_active": is_active,
		"is_active_bool": bool(is_active),
		"is_featured": is_featured,
		"is_featured_bool": bool(is_featured),
		"contact_person": contact_person,
		"email": email,
		"phone": phone,
		"created_at": creation,
		"updated_at": modified,
	}


SPONSORS_CACHE_KEY = "connectfest:sponsors:active_default"


def invalidate_sponsors_cache():
	"""Invalidates Redis cache keys for sponsors."""
	try:
		cache = frappe.cache()
		cache.delete(cache.make_key(SPONSORS_CACHE_KEY))
	except Exception:
		pass


BLOCKED_IPS_CACHE_KEY = "connectfest:blocked_ips_set"


def invalidate_blocked_ips_cache():
	"""Invalidates Redis cache for blocked IPs."""
	try:
		cache = frappe.cache()
		cache.delete(cache.make_key(BLOCKED_IPS_CACHE_KEY))
	except Exception:
		pass


def get_client_ip(override_ip=None):
	"""Extracts client IP safely, prioritizing override (for tests) or request headers."""
	if override_ip and str(override_ip).strip():
		return str(override_ip).strip()
	ip = None
	if getattr(frappe, "request", None):
		x_forwarded = frappe.request.headers.get("X-Forwarded-For")
		if x_forwarded:
			ip = str(x_forwarded).split(",")[0].strip()
		elif hasattr(frappe.request, "remote_addr") and frappe.request.remote_addr:
			ip = str(frappe.request.remote_addr).strip()
	if not ip and getattr(frappe, "local", None) and getattr(frappe.local, "request_ip", None):
		ip = str(frappe.local.request_ip).strip()
	return ip or "127.0.0.1"


def is_ip_blocked(ip):
	"""Checks whether an IP is currently blocked."""
	if not ip:
		return False
	ip = str(ip).strip()

	try:
		cache = frappe.cache()
		cached = cache.get(cache.make_key(BLOCKED_IPS_CACHE_KEY))
		if cached:
			if isinstance(cached, bytes):
				cached = cached.decode("utf-8")
			b_set = set(json.loads(cached) if isinstance(cached, str) else cached)
			return ip in b_set
	except Exception:
		pass

	try:
		blocked_ips = frappe.db.sql_list("SELECT ip_address FROM `tabConnectFest Blocked IP` WHERE is_active = 1")
		try:
			cache = frappe.cache()
			cache.setex(cache.make_key(BLOCKED_IPS_CACHE_KEY), 300, json.dumps(blocked_ips))
		except Exception:
			pass
		return ip in blocked_ips
	except Exception:
		return False


def block_ip_address(ip, reason="Blocked by Administrator", blocked_from=None, blocked_by=None):
	"""Adds or activates an IP in the blocked list."""
	if not ip:
		return None
	ip = str(ip).strip()
	existing_name = frappe.db.get_value("ConnectFest Blocked IP", {"ip_address": ip}, "name")
	if existing_name:
		doc = frappe.get_doc("ConnectFest Blocked IP", existing_name)
		doc.is_active = 1
		doc.reason = reason or doc.reason
		doc.blocked_from = blocked_from or doc.blocked_from
		doc.blocked_by = blocked_by or frappe.session.user or "Administrator"
		doc.blocked_at = frappe.utils.now_datetime()
		doc.save(ignore_permissions=True)
	else:
		doc = frappe.get_doc({
			"doctype": "ConnectFest Blocked IP",
			"ip_address": ip,
			"is_active": 1,
			"reason": reason,
			"blocked_from": blocked_from,
			"blocked_by": blocked_by or frappe.session.user or "Administrator",
			"blocked_at": frappe.utils.now_datetime(),
		})
		doc.insert(ignore_permissions=True)
	frappe.db.commit()
	invalidate_blocked_ips_cache()
	return doc


def unblock_ip_address(ip):
	"""Unblocks an IP address."""
	if not ip:
		return False
	ip = str(ip).strip()
	frappe.db.sql(
		"UPDATE `tabConnectFest Blocked IP` SET is_active = 0 WHERE ip_address = %s",
		(ip,)
	)
	frappe.db.commit()
	invalidate_blocked_ips_cache()
	return True


def _format_sponsor_registration(doc_or_dict):
	"""Standardizes sponsor registration output dictionary for API and frontend JSON consumption."""
	if isinstance(doc_or_dict, dict):
		name = doc_or_dict.get("name")
		reg_id = doc_or_dict.get("registration_id") or name
		company_name = doc_or_dict.get("company_name") or ""
		contact_person = doc_or_dict.get("contact_person") or ""
		email = doc_or_dict.get("email") or ""
		mobile_no = doc_or_dict.get("mobile_no") or ""
		logo = doc_or_dict.get("logo") or ""
		website_url = doc_or_dict.get("website_url") or ""
		sponsor_category = doc_or_dict.get("sponsor_category") or "Associate Sponsor"
		sponsored_for = doc_or_dict.get("sponsored_for") or "Event Sponsor"
		status = doc_or_dict.get("status") or "Pending"
		ip_address = doc_or_dict.get("ip_address") or ""
		action_by = doc_or_dict.get("action_by") or ""
		action_at = str(doc_or_dict.get("action_at") or "")
		created_sponsor = doc_or_dict.get("created_sponsor") or ""
		creation = str(doc_or_dict.get("creation") or "")
		modified = str(doc_or_dict.get("modified") or "")
	else:
		name = doc_or_dict.name
		reg_id = getattr(doc_or_dict, "registration_id", None) or name
		company_name = getattr(doc_or_dict, "company_name", "") or ""
		contact_person = getattr(doc_or_dict, "contact_person", "") or ""
		email = getattr(doc_or_dict, "email", "") or ""
		mobile_no = getattr(doc_or_dict, "mobile_no", "") or ""
		logo = getattr(doc_or_dict, "logo", "") or ""
		website_url = getattr(doc_or_dict, "website_url", "") or ""
		sponsor_category = getattr(doc_or_dict, "sponsor_category", "") or "Associate Sponsor"
		sponsored_for = getattr(doc_or_dict, "sponsored_for", "") or "Event Sponsor"
		status = getattr(doc_or_dict, "status", "") or "Pending"
		ip_address = getattr(doc_or_dict, "ip_address", "") or ""
		action_by = getattr(doc_or_dict, "action_by", "") or ""
		action_at = str(getattr(doc_or_dict, "action_at", "") or "")
		created_sponsor = getattr(doc_or_dict, "created_sponsor", "") or ""
		creation = str(getattr(doc_or_dict, "creation", "") or "")
		modified = str(getattr(doc_or_dict, "modified", "") or "")

	is_blocked = 1 if (status == "Blocked" or is_ip_blocked(ip_address)) else 0

	return {
		"id": name,
		"name": name,
		"registration_id": reg_id,
		"company_name": company_name,
		"contact_person": contact_person,
		"email": email,
		"mobile": mobile_no,
		"mobile_no": mobile_no,
		"logo": format_full_url(logo),
		"website_url": website_url,
		"sponsor_category": sponsor_category,
		"sponsored_for": sponsored_for,
		"status": status,
		"ip_address": ip_address,
		"block": is_blocked,
		"is_blocked": is_blocked,
		"action_by": action_by or None,
		"action_at": action_at or None,
		"created_sponsor": created_sponsor or None,
		"created_at": creation,
		"updated_at": modified,
	}


# ==============================================================================
# 1. PUBLIC WEBSITE SPONSORS APIS (For Visitors / Attendees / Public)
# ==============================================================================

@handle_api_errors("ConnectFest Public Get Sponsors Error")
def get_sponsors(**kwargs):
	"""
	Public API for User Website to view all event sponsors.
	- Whitelisted for Guests (Public).
	- Returns only active (is_active = 1) sponsors.
	- Sorted by: is_featured DESC, display_order ASC, creation ASC (Featured first).
	- Returns:
	  * 'sponsors': Complete flat list
	  * 'grouped_by_category': Sponsors categorized under tiers (Title, Platinum, Gold, etc.)
	  * 'categories': Available category list for tabs/filters
	- Redis cached for sub-millisecond response time.
	"""
	data = extract_request_data(kwargs)
	category = (data.get("category") or data.get("sponsor_category") or data.get("tier") or "").strip()
	search = (data.get("search") or data.get("query") or "").strip().lower()
	featured_only = data.get("is_featured") or data.get("featured")

	raw_page = data.get("page")
	raw_limit = data.get("limit") or data.get("page_size")
	page = max(1, cint(raw_page)) if raw_page else 1
	limit = min(200, max(1, cint(raw_limit))) if raw_limit else 0
	offset = (page - 1) * limit if limit > 0 else 0

	is_default_query = not category and not search and featured_only is None and page == 1 and limit == 0
	cache = frappe.cache()
	cache_key = cache.make_key(SPONSORS_CACHE_KEY)

	if is_default_query:
		try:
			cached = cache.get(cache_key)
			if cached:
				if isinstance(cached, bytes):
					cached = cached.decode("utf-8")
				return json.loads(cached)
		except Exception:
			pass

	conditions = ["is_active = 1"]
	params = {}

	if category and category.lower() != "all":
		conditions.append("LOWER(sponsor_category) = %(cat)s")
		params["cat"] = category.lower()

	if featured_only is not None and str(featured_only).strip() != "":
		conditions.append("is_featured = %(featured)s")
		params["featured"] = parse_boolean(featured_only)

	if search:
		conditions.append("(LOWER(sponsor_name) LIKE %(q)s OR LOWER(sponsored_for) LIKE %(q)s OR LOWER(sponsor_category) LIKE %(q)s OR LOWER(description) LIKE %(q)s)")
		params["q"] = f"%{search}%"

	where_clause = " AND ".join(conditions)

	total_count = frappe.db.sql(f"SELECT COUNT(*) FROM `tabConnectFest Sponsor` WHERE {where_clause}", params)[0][0]
	featured_count = frappe.db.sql("SELECT COUNT(*) FROM `tabConnectFest Sponsor` WHERE is_active = 1 AND is_featured = 1")[0][0]

	limit_sql = f"LIMIT {limit} OFFSET {offset}" if limit > 0 else ""
	query = f"""
		SELECT name, sponsor_name, sponsor_category, sponsored_for, logo, website_url,
		       stall_number, description, display_order, is_active, is_featured,
		       contact_person, email, phone, creation, modified
		FROM `tabConnectFest Sponsor`
		WHERE {where_clause}
		ORDER BY is_featured DESC, display_order ASC, creation ASC
		{limit_sql}
	"""
	rows = frappe.db.sql(query, params, as_dict=True)
	formatted_sponsors = [_format_sponsor_doc(r) for r in rows]

	# Fetch distinct categories for UI filter tabs
	db_cats = frappe.db.sql("""
		SELECT DISTINCT sponsor_category FROM `tabConnectFest Sponsor`
		WHERE is_active = 1 AND sponsor_category IS NOT NULL AND sponsor_category != ''
		ORDER BY sponsor_category ASC
	""", as_dict=True)
	available_categories = ["All"] + [c.sponsor_category for c in db_cats]

	res = {
		"status": "success",
		"total_sponsors": total_count,
		"featured_count": featured_count,
		"categories": available_categories,
		"active_category": category or "All",
		"sponsors": formatted_sponsors,
	}

	if is_default_query:
		try:
			cache.setex(cache_key, 300, json.dumps(res, default=str))
		except Exception:
			pass

	return res


@handle_api_errors("ConnectFest Public Get Sponsor Details Error")
def get_sponsor_details(sponsor_id=None, name=None, **kwargs):
	"""
	Public API to view detailed profile of a single sponsor.
	"""
	data = extract_request_data(kwargs)
	docname = (sponsor_id or name or data.get("sponsor_id") or data.get("name") or data.get("id") or "").strip()

	if not docname:
		frappe.local.response["http_status_code"] = 400
		return {"status": "error", "message": "sponsor_id is required"}

	if not frappe.db.exists("ConnectFest Sponsor", docname):
		frappe.local.response["http_status_code"] = 404
		return {"status": "error", "message": f"Sponsor '{docname}' not found"}

	doc = frappe.get_doc("ConnectFest Sponsor", docname)
	if not doc.is_active:
		frappe.local.response["http_status_code"] = 404
		return {"status": "error", "message": f"Sponsor '{docname}' is currently inactive"}

	return {
		"status": "success",
		"sponsor": _format_sponsor_doc(doc),
	}


# ==============================================================================
# 2. ADMIN SPONSORS APIS (Add, Edit, List, Delete & Pin Sponsors)
# ==============================================================================

@handle_api_errors("ConnectFest Admin Create Sponsor Error")
def admin_create_sponsor(sponsor_name=None, sponsor_category=None, sponsored_for=None, logo=None, website_url=None, stall_number=None, is_active=None, is_featured=None, featured=None, display_order=None, description=None, contact_person=None, email=None, phone=None, **kwargs):
	"""
	Admin API to add a new sponsor and what they sponsored for.
	"""
	err = check_admin_permission("sponsor management")
	if err:
		return err

	data = extract_request_data(kwargs)
	s_name = (sponsor_name or data.get("sponsor_name") or data.get("company_name") or data.get("name") or "").strip()
	s_category = (sponsor_category or data.get("sponsor_category") or data.get("category") or data.get("tier") or "Associate Sponsor").strip()
	s_for = (sponsored_for or data.get("sponsored_for") or data.get("purpose") or data.get("sponsorship_type") or "").strip()
	s_url = (website_url or data.get("website_url") or data.get("website") or data.get("url") or "").strip()
	s_stall = (stall_number or data.get("stall_number") or data.get("stall_no") or "").strip()
	s_desc = (description or data.get("description") or "").strip()
	s_contact = (contact_person or data.get("contact_person") or data.get("contact") or "").strip()
	s_email = (email or data.get("email") or "").strip().lower()
	s_phone = (phone or data.get("phone") or data.get("mobile_no") or "").strip()

	raw_featured = (
		data.get("is_featured")
		if "is_featured" in data
		else (data.get("featured") if "featured" in data else (is_featured if is_featured is not None else (featured if featured is not None else 0)))
	)
	s_featured = parse_boolean(raw_featured)

	raw_active = (
		data.get("is_active")
		if "is_active" in data
		else (data.get("active") if "active" in data else (is_active if is_active is not None else 1))
	)
	s_active = parse_boolean(raw_active)

	raw_order = (
		data.get("display_order")
		if "display_order" in data
		else (data.get("order") if "order" in data else (display_order if display_order is not None else None))
	)
	if raw_order is not None and str(raw_order).strip() != "":
		s_order = cint(raw_order)
	else:
		max_order = frappe.db.sql("SELECT COALESCE(MAX(display_order), 0) FROM `tabConnectFest Sponsor`")[0][0]
		s_order = max_order + 1

	if not s_name:
		frappe.local.response["http_status_code"] = 400
		return {"status": "error", "message": "sponsor_name is required"}

	if not s_for:
		frappe.local.response["http_status_code"] = 400
		return {"status": "error", "message": "sponsored_for is required (e.g. 'Title Sponsor', 'Day 1 Lunch Sponsor', 'Official Badges & Lanyards')"}

	# Handle logo file/base64 upload
	raw_logo = logo or data.get("logo") or data.get("image") or data.get("logo_url") or data.get("image_url")
	saved_logo = ""
	if raw_logo:
		saved_logo = save_image_from_base64_or_url(raw_logo, file_name_prefix=f"sponsor_{uuid.uuid4().hex[:6]}")

	# Ensure Top Banner uniqueness: exactly 1 top banner sponsor allowed (no duplicates)
	if s_featured:
		frappe.db.sql("UPDATE `tabConnectFest Sponsor` SET is_featured = 0 WHERE is_featured = 1")

	doc = frappe.get_doc({
		"doctype": "ConnectFest Sponsor",
		"sponsor_name": s_name,
		"sponsor_category": s_category,
		"sponsored_for": s_for,
		"logo": saved_logo or (raw_logo if isinstance(raw_logo, str) and raw_logo.startswith("/files/") else ""),
		"website_url": s_url,
		"stall_number": s_stall if s_stall and frappe.db.exists("ConnectFest Stall", s_stall) else None,
		"description": s_desc,
		"is_active": s_active,
		"is_featured": s_featured,
		"display_order": s_order,
		"contact_person": s_contact,
		"email": s_email,
		"phone": s_phone,
	})
	doc.flags.ignore_permissions = True
	doc.insert()
	frappe.db.commit()
	invalidate_sponsors_cache()

	return {
		"status": "success",
		"message": f"Sponsor '{doc.sponsor_name}' added successfully ({doc.sponsored_for})" + (" (⭐ Featured)" if doc.is_featured else ""),
		"sponsor": _format_sponsor_doc(doc),
	}


@handle_api_errors("ConnectFest Admin Get Sponsors Error")
def admin_get_sponsors(**kwargs):
	"""
	Admin API to view all sponsors (both active and inactive) with filters and summary.
	"""
	err = check_admin_permission("sponsor management")
	if err:
		return err

	data = extract_request_data(kwargs)
	category = (data.get("category") or data.get("sponsor_category") or "").strip()
	search = (data.get("search") or data.get("query") or "").strip().lower()
	active_filter = data.get("is_active") or data.get("status")
	featured_filter = data.get("is_featured")

	raw_page = data.get("page")
	raw_limit = data.get("limit") or data.get("page_size") or 50
	page = max(1, cint(raw_page)) if raw_page else 1
	limit = min(200, max(1, cint(raw_limit))) if raw_limit else 50
	offset = (page - 1) * limit

	conditions = ["1=1"]
	params = {}

	if category and category.lower() != "all":
		conditions.append("LOWER(sponsor_category) = %(cat)s")
		params["cat"] = category.lower()

	if active_filter is not None and str(active_filter).strip() != "":
		conditions.append("is_active = %(active)s")
		params["active"] = parse_boolean(active_filter)

	if featured_filter is not None and str(featured_filter).strip() != "":
		conditions.append("is_featured = %(featured)s")
		params["featured"] = parse_boolean(featured_filter)

	if search:
		conditions.append("(LOWER(sponsor_name) LIKE %(q)s OR LOWER(sponsored_for) LIKE %(q)s OR LOWER(sponsor_category) LIKE %(q)s OR LOWER(description) LIKE %(q)s OR LOWER(email) LIKE %(q)s)")
		params["q"] = f"%{search}%"

	where_clause = " AND ".join(conditions)

	total_count = frappe.db.sql(f"SELECT COUNT(*) FROM `tabConnectFest Sponsor` WHERE {where_clause}", params)[0][0]
	active_count = frappe.db.sql("SELECT COUNT(*) FROM `tabConnectFest Sponsor` WHERE is_active = 1")[0][0]
	inactive_count = frappe.db.sql("SELECT COUNT(*) FROM `tabConnectFest Sponsor` WHERE is_active = 0")[0][0]
	featured_count = frappe.db.sql("SELECT COUNT(*) FROM `tabConnectFest Sponsor` WHERE is_featured = 1")[0][0]

	rows = frappe.db.sql(f"""
		SELECT name, sponsor_name, sponsor_category, sponsored_for, logo, website_url,
		       stall_number, description, display_order, is_active, is_featured,
		       contact_person, email, phone, creation, modified
		FROM `tabConnectFest Sponsor`
		WHERE {where_clause}
		ORDER BY is_featured DESC, display_order ASC, creation DESC
		LIMIT %(limit)s OFFSET %(offset)s
	""", {**params, "limit": limit, "offset": offset}, as_dict=True)

	formatted_sponsors = [_format_sponsor_doc(r) for r in rows]

	return {
		"status": "success",
		"total_sponsors": total_count,
		"summary": {
			"total_all": total_count,
			"active_sponsors": active_count,
			"inactive_sponsors": inactive_count,
			"featured_sponsors": featured_count,
		},
		"pagination": {
			"current_page": page,
			"page_size": limit,
			"total_items": total_count,
			"total_pages": (total_count + limit - 1) // limit if limit else 1,
			"has_next": (offset + limit) < total_count,
			"has_prev": page > 1,
		},
		"sponsors": formatted_sponsors,
	}


@handle_api_errors("ConnectFest Admin Update Sponsor Error")
def admin_update_sponsor(sponsor_id=None, name=None, sponsor_name=None, sponsor_category=None, sponsored_for=None, logo=None, website_url=None, stall_number=None, is_active=None, is_featured=None, display_order=None, description=None, contact_person=None, email=None, phone=None, **kwargs):
	"""
	Admin API to update existing sponsor information.
	"""
	err = check_admin_permission("sponsor management")
	if err:
		return err

	data = extract_request_data(kwargs)
	docname = (sponsor_id or name or data.get("sponsor_id") or data.get("name") or data.get("id") or "").strip()

	if not docname:
		frappe.local.response["http_status_code"] = 400
		return {"status": "error", "message": "sponsor_id is required"}

	if not frappe.db.exists("ConnectFest Sponsor", docname):
		frappe.local.response["http_status_code"] = 404
		return {"status": "error", "message": f"Sponsor '{docname}' not found"}

	doc = frappe.get_doc("ConnectFest Sponsor", docname)

	if sponsor_name is not None or "sponsor_name" in data or "company_name" in data:
		doc.sponsor_name = (sponsor_name or data.get("sponsor_name") or data.get("company_name") or "").strip()
	if sponsor_category is not None or "sponsor_category" in data or "category" in data:
		doc.sponsor_category = (sponsor_category or data.get("sponsor_category") or data.get("category") or "").strip()
	if sponsored_for is not None or "sponsored_for" in data or "purpose" in data:
		doc.sponsored_for = (sponsored_for or data.get("sponsored_for") or data.get("purpose") or "").strip()
	if website_url is not None or "website_url" in data or "website" in data:
		doc.website_url = (website_url or data.get("website_url") or data.get("website") or "").strip()
	if stall_number is not None or "stall_number" in data:
		st = (stall_number or data.get("stall_number") or "").strip()
		doc.stall_number = st if st and frappe.db.exists("ConnectFest Stall", st) else None
	if hasattr(doc, "description") and (description is not None or "description" in data):
		doc.description = (description or data.get("description") or "").strip()
	if contact_person is not None or "contact_person" in data:
		doc.contact_person = (contact_person or data.get("contact_person") or "").strip()
	if email is not None or "email" in data:
		doc.email = (email or data.get("email") or "").strip().lower()
	if phone is not None or "phone" in data:
		doc.phone = (phone or data.get("phone") or "").strip()
	if is_active is not None or "is_active" in data:
		doc.is_active = parse_boolean(is_active if is_active is not None else data.get("is_active"))
	if is_featured is not None or "is_featured" in data:
		doc.is_featured = parse_boolean(is_featured if is_featured is not None else data.get("is_featured"))
	if display_order is not None or "display_order" in data:
		doc.display_order = cint(display_order if display_order is not None else data.get("display_order"))

	# Logo update
	raw_logo = logo if logo is not None else data.get("logo")
	if raw_logo is not None:
		saved_logo = save_image_from_base64_or_url(raw_logo, file_name_prefix=f"sponsor_{docname}")
		if saved_logo:
			doc.logo = saved_logo

	doc.flags.ignore_permissions = True
	doc.save()
	frappe.db.commit()
	invalidate_sponsors_cache()

	return {
		"status": "success",
		"message": f"Sponsor '{doc.sponsor_name}' updated successfully",
		"sponsor": _format_sponsor_doc(doc),
	}


@handle_api_errors("ConnectFest Admin Delete Sponsor Error")
def admin_delete_sponsor(sponsor_id=None, name=None, sponsor_ids=None, ids=None, **kwargs):
	"""
	Admin API to delete one or multiple sponsors.
	Supports single sponsor_id or array/comma-separated sponsor_ids for bulk deletion.
	"""
	err = check_admin_permission("sponsor management")
	if err:
		return err

	data = extract_request_data(kwargs)
	raw_ids = sponsor_ids or ids or data.get("sponsor_ids") or data.get("ids") or data.get("items")
	single_id = (sponsor_id or name or data.get("sponsor_id") or data.get("name") or data.get("id") or "").strip()

	target_ids = []
	if raw_ids:
		target_ids.extend(_extract_id_list(raw_ids))
	if single_id:
		target_ids.extend(_extract_id_list(single_id))

	seen = set()
	clean_ids = []
	for sid in target_ids:
		if sid and sid not in seen:
			seen.add(sid)
			clean_ids.append(sid)

	if not clean_ids:
		frappe.local.response["http_status_code"] = 400
		return {"status": "error", "message": "sponsor_id or sponsor_ids is required"}

	deleted_ids = []
	not_found_ids = []
	deleted_names = []

	for sid in clean_ids:
		if frappe.db.exists("ConnectFest Sponsor", sid):
			sname = frappe.db.get_value("ConnectFest Sponsor", sid, "sponsor_name") or sid
			try:
				frappe.delete_doc("ConnectFest Sponsor", sid, ignore_permissions=True, force=True)
				deleted_ids.append(sid)
				deleted_names.append(sname)
			except Exception as ex:
				not_found_ids.append({"id": sid, "reason": str(ex)})
		else:
			not_found_ids.append({"id": sid, "reason": "Not found"})

	frappe.db.commit()
	invalidate_sponsors_cache()

	if not deleted_ids:
		frappe.local.response["http_status_code"] = 404
		return {
			"status": "error",
			"message": "None of the specified sponsors were found",
			"failed_ids": not_found_ids,
		}

	msg = (
		f"Sponsor '{deleted_names[0]}' ({deleted_ids[0]}) has been deleted successfully"
		if len(deleted_ids) == 1
		else f"Successfully deleted {len(deleted_ids)} sponsor(s)"
	)

	return {
		"status": "success",
		"message": msg,
		"deleted_count": len(deleted_ids),
		"deleted_id": deleted_ids[0] if len(deleted_ids) == 1 else None,
		"deleted_ids": deleted_ids,
		"failed_ids": not_found_ids if not_found_ids else [],
	}


# Alias for bulk deletion
admin_delete_sponsors = admin_delete_sponsor


@handle_api_errors("ConnectFest Admin Toggle Sponsor Status Error")
def admin_toggle_sponsor_status(sponsor_id=None, name=None, sponsor_ids=None, ids=None, is_active=None, is_featured=None, **kwargs):
	"""
	Admin API to toggle or set active or featured status for one or multiple sponsors.
	"""
	err = check_admin_permission("sponsor management")
	if err:
		return err

	data = extract_request_data(kwargs)
	raw_ids = sponsor_ids or ids or data.get("sponsor_ids") or data.get("ids") or data.get("items")
	single_id = (sponsor_id or name or data.get("sponsor_id") or data.get("name") or data.get("id") or "").strip()

	target_ids = []
	if raw_ids:
		target_ids.extend(_extract_id_list(raw_ids))
	if single_id:
		target_ids.extend(_extract_id_list(single_id))

	seen = set()
	clean_ids = []
	for sid in target_ids:
		if sid and sid not in seen:
			seen.add(sid)
			clean_ids.append(sid)

	if not clean_ids:
		frappe.local.response["http_status_code"] = 400
		return {"status": "error", "message": "sponsor_id or sponsor_ids is required"}

	active_input = is_active if is_active is not None else data.get("is_active")
	featured_input = is_featured if is_featured is not None else data.get("is_featured")

	updated_ids = []
	not_found_ids = []

	for sid in clean_ids:
		if not frappe.db.exists("ConnectFest Sponsor", sid):
			not_found_ids.append(sid)
			continue

		doc = frappe.get_doc("ConnectFest Sponsor", sid)
		if active_input is not None and str(active_input).strip() != "":
			doc.is_active = parse_boolean(active_input)
		elif featured_input is None and len(clean_ids) == 1:
			doc.is_active = 0 if doc.is_active else 1

		if featured_input is not None and str(featured_input).strip() != "":
			doc.is_featured = parse_boolean(featured_input)
		elif active_input is None and len(clean_ids) == 1:
			doc.is_featured = 0 if doc.is_featured else 1

		doc.flags.ignore_permissions = True
		doc.save()
		updated_ids.append(sid)

	frappe.db.commit()
	invalidate_sponsors_cache()

	if not updated_ids:
		frappe.local.response["http_status_code"] = 404
		return {
			"status": "error",
			"message": "None of the specified sponsors were found",
			"failed_ids": not_found_ids,
		}

	return {
		"status": "success",
		"message": f"Updated status for {len(updated_ids)} sponsor(s)",
		"updated_count": len(updated_ids),
		"updated_ids": updated_ids,
		"failed_ids": not_found_ids,
	}


def setup_default_sponsors():
	"""
	Sets up default realistic sponsors for ConnectFest 2027 if none exist.
	"""
	if not frappe.db.table_exists("ConnectFest Sponsor"):
		return

	if frappe.db.count("ConnectFest Sponsor") > 0:
		return

	default_sponsors = [
		{
			"sponsor_name": "Zoho Corporation",
			"sponsor_category": "Title Sponsor",
			"sponsored_for": "Grand Title Event Sponsor & Official Cloud Partner",
			"logo": "/files/zoho_logo.png",
			"website_url": "https://zoho.com",
			"stall_number": "TS-01",
			"is_featured": 1,
			"display_order": 1,
			"description": "Global technology company offering comprehensive business software solutions.",
		},
		{
			"sponsor_name": "Tata Communications",
			"sponsor_category": "Platinum Sponsor",
			"sponsored_for": "High-Speed Wi-Fi & Event Digital Connectivity",
			"logo": "/files/tata_logo.png",
			"website_url": "https://tatacommunications.com",
			"is_featured": 1,
			"display_order": 2,
			"description": "Leading digital ecosystem enabler empowering enterprises globally.",
		},
		{
			"sponsor_name": "Nakshatra Fabricators",
			"sponsor_category": "Gold Sponsor",
			"sponsored_for": "Grand Expo Stage Design & Audio Visuals",
			"logo": "/files/nakshatra_logo.png",
			"website_url": "https://nakshatrafab.com",
			"stall_number": "A-01",
			"is_featured": 1,
			"display_order": 3,
			"description": "Pioneers in high-precision industrial fabrication and stage structures.",
		},
		{
			"sponsor_name": "Sri Vel Interiors",
			"sponsor_category": "Associate Sponsor",
			"sponsored_for": "Official Attendee Badges & Delegate Lanyards",
			"logo": "/files/srivel_logo.png",
			"website_url": "https://srivelinteriors.com",
			"stall_number": "A-03",
			"is_featured": 0,
			"display_order": 4,
			"description": "Turnkey commercial and corporate interior solutions provider.",
		},
		{
			"sponsor_name": "Aachi Food Products",
			"sponsor_category": "Refreshment Partner",
			"sponsored_for": "Official Networking Lunch & Refreshments Sponsor",
			"logo": "/files/aachi_logo.png",
			"website_url": "https://aachigroup.com",
			"is_featured": 0,
			"display_order": 5,
			"description": "Household brand celebrating authentic culinary delights across Tamil Nadu.",
		},
	]

	for sp in default_sponsors:
		doc = frappe.new_doc("ConnectFest Sponsor")
		doc.update(sp)
		doc.is_active = 1
		doc.flags.ignore_permissions = True
		doc.insert()

	frappe.db.commit()
	print("ConnectFest: Initialized 5 official ConnectFest 2027 sponsors.")


# Backward-compatibility alias
seed_default_sponsors = setup_default_sponsors


# ==============================================================================
# 3. BECOME A SPONSOR PUBLIC REGISTRATION & ADMIN REVIEW WORKFLOW
# ==============================================================================

@handle_api_errors("ConnectFest Become A Sponsor Registration Error")
def register_sponsor_request(
	name=None,
	company_name=None,
	sponsor_name=None,
	contact_person=None,
	mobile=None,
	mobile_no=None,
	phone=None,
	email=None,
	logo=None,
	company_logo=None,
	website_url=None,
	company_url=None,
	website=None,
	sponsor_category=None,
	category=None,
	tier=None,
	sponsored_for=None,
	purpose=None,
	notes=None,
	message=None,
	ip_address=None,
	client_ip=None,
	**kwargs
):
	"""
	Public API: Form submission for 'Become a Sponsor'.
	- Whitelisted for Guests (Public).
	- Fields collected:
	  * Name / Contact Person
	  * Company Name
	  * Mobile Number
	  * Email Address
	  * Company Logo (file upload, base64 data URL, or public file URL)
	  * Company URL (website)
	  * (Optional) Sponsor Category / Tier, Sponsored For / Purpose, Notes
	- IP Protection:
	  * Checks caller's IP against Blocked IPs list. If blocked:
	    Returns HTTP 403: 'Requests from your IP address have been blocked by administrator.'
	- Creates a record in 'ConnectFest Sponsor Registration' with status 'Pending'.
	"""
	data = extract_request_data(kwargs)

	# 1. IP Blocking Check
	client_ip_val = get_client_ip(
		ip_address or client_ip or data.get("ip_address") or data.get("client_ip")
	)

	if is_ip_blocked(client_ip_val):
		frappe.local.response["http_status_code"] = 403
		return {
			"status": "error",
			"blocked": True,
			"ip_address": client_ip_val,
			"message": "Requests from your IP address have been blocked by administrator.",
		}

	# 2. Extract inputs
	c_name = (company_name or sponsor_name or data.get("company_name") or data.get("sponsor_name") or "").strip()
	person_name = (name or contact_person or data.get("name") or data.get("contact_person") or "").strip()
	c_email = (email or data.get("email") or "").strip().lower()
	c_mobile = (mobile or mobile_no or phone or data.get("mobile") or data.get("mobile_no") or data.get("phone") or "").strip()
	c_url = (website_url or company_url or website or data.get("website_url") or data.get("company_url") or data.get("website") or "").strip()
	c_category = (sponsor_category or category or tier or data.get("sponsor_category") or data.get("category") or data.get("tier") or "Associate Sponsor").strip()
	c_for = (sponsored_for or purpose or data.get("sponsored_for") or data.get("purpose") or "Event Sponsor").strip()
	c_notes = (notes or message or data.get("notes") or data.get("message") or "").strip()

	# 3. Validations
	if not c_name:
		frappe.local.response["http_status_code"] = 400
		return {"status": "error", "message": "Company Name is required"}

	if not person_name:
		frappe.local.response["http_status_code"] = 400
		return {"status": "error", "message": "Contact Person / Name is required"}

	if not c_email:
		frappe.local.response["http_status_code"] = 400
		return {"status": "error", "message": "Email address is required"}

	if not validate_email_address(c_email, throw=False):
		frappe.local.response["http_status_code"] = 400
		return {"status": "error", "message": "Invalid email address format. Please enter a valid email."}

	if not c_mobile:
		frappe.local.response["http_status_code"] = 400
		return {"status": "error", "message": "Mobile number is required"}

	clean_mobile = "".join(filter(str.isdigit, c_mobile))
	if len(clean_mobile) == 12 and clean_mobile.startswith("91"):
		clean_mobile = clean_mobile[2:]
	if len(clean_mobile) != 10:
		frappe.local.response["http_status_code"] = 400
		return {"status": "error", "message": "Please provide a valid 10-digit mobile number"}

	# 4. Handle Logo file / Base64 upload
	raw_logo = logo or company_logo or data.get("logo") or data.get("company_logo") or data.get("image")
	saved_logo = ""
	if raw_logo:
		saved_logo = save_image_from_base64_or_url(raw_logo, file_name_prefix=f"sponsor_app_{uuid.uuid4().hex[:6]}")

	# 5. Create Registration Document
	doc = frappe.get_doc({
		"doctype": "ConnectFest Sponsor Registration",
		"company_name": c_name,
		"contact_person": person_name,
		"email": c_email,
		"mobile_no": clean_mobile,
		"logo": saved_logo or (raw_logo if isinstance(raw_logo, str) and raw_logo.startswith("/files/") else ""),
		"website_url": c_url,
		"sponsor_category": c_category,
		"sponsored_for": c_for,
		"status": "Pending",
		"ip_address": client_ip_val,
	})
	doc.flags.ignore_permissions = True
	doc.insert()
	frappe.db.commit()

	return {
		"status": "success",
		"message": f"Thank you for registering to Become a Sponsor! Your application for '{doc.company_name}' has been received and is pending admin review.",
		"registration": _format_sponsor_registration(doc),
	}


# Alias for become_a_sponsor
become_a_sponsor = register_sponsor_request


@handle_api_errors("ConnectFest Admin Action Sponsor Registration Error")
def admin_action_sponsor_registration(
	registration_id=None,
	name=None,
	action=None,
	status=None,
	notes=None,
	reason=None,
	sponsor_category=None,
	category=None,
	tier=None,
	sponsored_for=None,
	purpose=None,
	stall_number=None,
	is_featured=None,
	featured=None,
	display_order=None,
	**kwargs
):
	"""
	Admin API to review and process a Sponsor Registration:
	- Actions:
	  * 'accept': Marks application as 'Accepted', adds new 'ConnectFest Sponsor' record.
	    Ensures only 1 Top Banner exists (no duplicate top banners).
	  * 'reject': Marks application as 'Rejected'. Does not create sponsor.
	  * 'block': Marks application as 'Blocked' and permanently blocks client's IP from submitting requests.
	"""
	err = check_admin_permission("sponsor registration management")
	if err:
		return err

	data = extract_request_data(kwargs)
	docname = (registration_id or name or data.get("registration_id") or data.get("name") or data.get("id") or "").strip()
	chosen_action = (action or status or data.get("action") or data.get("status") or "").strip().lower()
	action_notes = (notes or reason or data.get("notes") or data.get("reason") or "").strip()

	if not docname:
		frappe.local.response["http_status_code"] = 400
		return {"status": "error", "message": "registration_id is required"}

	if not chosen_action:
		frappe.local.response["http_status_code"] = 400
		return {"status": "error", "message": "action is required ('accept', 'reject', or 'block')"}

	if chosen_action not in ("accept", "accepted", "approve", "approved", "reject", "rejected", "block", "blocked"):
		frappe.local.response["http_status_code"] = 400
		return {"status": "error", "message": f"Invalid action '{chosen_action}'. Allowed: 'accept', 'reject', 'block'"}

	if not frappe.db.exists("ConnectFest Sponsor Registration", docname):
		frappe.local.response["http_status_code"] = 404
		return {"status": "error", "message": f"Sponsor registration '{docname}' not found"}

	reg = frappe.get_doc("ConnectFest Sponsor Registration", docname)

	if chosen_action in ("accept", "accepted", "approve", "approved"):
		action_type = "accept"
	elif chosen_action in ("reject", "rejected"):
		action_type = "reject"
	else:
		action_type = "block"

	if action_type == "accept":
		# State Rule: Cannot accept an application that is Blocked or from a blocked IP
		if reg.status == "Blocked" or (reg.ip_address and is_ip_blocked(reg.ip_address)):
			frappe.local.response["http_status_code"] = 400
			return {
				"status": "error",
				"message": f"Cannot accept application '{reg.name}': It is currently blocked. Blocked applications cannot be accepted.",
			}

		# State Rule: Cannot accept an application that has been Rejected
		if reg.status == "Rejected":
			frappe.local.response["http_status_code"] = 400
			return {
				"status": "error",
				"message": f"Cannot accept application '{reg.name}': It has already been rejected. Rejected applications cannot be accepted.",
			}

		if reg.status == "Accepted" and reg.created_sponsor and frappe.db.exists("ConnectFest Sponsor", reg.created_sponsor):
			frappe.local.response["http_status_code"] = 400
			return {
				"status": "error",
				"message": f"This registration is already accepted as sponsor '{reg.created_sponsor}'",
				"sponsor_id": reg.created_sponsor,
			}

		cat = (sponsor_category or category or tier or data.get("sponsor_category") or data.get("category") or data.get("tier") or reg.sponsor_category or "Associate Sponsor").strip()
		sp_for = (sponsored_for or purpose or data.get("sponsored_for") or data.get("purpose") or reg.sponsored_for or "Event Sponsor").strip()
		st_num = (stall_number or data.get("stall_number") or data.get("stall_no") or "").strip()
		raw_feat = is_featured if is_featured is not None else (featured if featured is not None else data.get("is_featured"))
		s_featured = parse_boolean(raw_feat)

		raw_order = display_order if display_order is not None else data.get("display_order")
		if raw_order is not None and str(raw_order).strip() != "":
			s_order = cint(raw_order)
		else:
			max_order = frappe.db.sql("SELECT COALESCE(MAX(display_order), 0) FROM `tabConnectFest Sponsor`")[0][0]
			s_order = max_order + 1

		# Ensure Top Banner uniqueness: exactly 1 top banner sponsor allowed (no duplicates)
		if s_featured:
			frappe.db.sql("UPDATE `tabConnectFest Sponsor` SET is_featured = 0 WHERE is_featured = 1")

		sponsor_doc = frappe.get_doc({
			"doctype": "ConnectFest Sponsor",
			"sponsor_name": reg.company_name,
			"contact_person": reg.contact_person,
			"email": reg.email,
			"phone": reg.mobile_no,
			"logo": reg.logo,
			"website_url": reg.website_url,
			"sponsor_category": cat,
			"sponsored_for": sp_for,
			"stall_number": st_num if st_num and frappe.db.exists("ConnectFest Stall", st_num) else None,
			"is_active": 1,
			"is_featured": 1 if s_featured else 0,
			"display_order": s_order,
		})
		sponsor_doc.flags.ignore_permissions = True
		sponsor_doc.insert()

		reg.status = "Accepted"
		reg.created_sponsor = sponsor_doc.name
		reg.action_by = frappe.session.user or "Administrator"
		reg.action_at = frappe.utils.now_datetime()
		reg.flags.ignore_permissions = True
		reg.save()
		frappe.db.commit()

		invalidate_sponsors_cache()

		return {
			"status": "success",
			"message": f"Sponsor '{reg.company_name}' accepted successfully and added as official sponsor '{sponsor_doc.name}'" + (" (⭐ Featured Top Banner)" if sponsor_doc.is_featured else ""),
			"sponsor": _format_sponsor_doc(sponsor_doc),
			"registration": _format_sponsor_registration(reg),
		}

	elif action_type == "reject":
		# State Rule: Cannot reject an application that is already Blocked
		if reg.status == "Blocked":
			frappe.local.response["http_status_code"] = 400
			return {
				"status": "error",
				"message": f"Cannot reject application '{reg.name}': It has already been Blocked.",
			}

		if reg.status == "Accepted":
			frappe.local.response["http_status_code"] = 400
			return {
				"status": "error",
				"message": f"Cannot reject application '{reg.name}': It has already been Accepted as official sponsor '{reg.created_sponsor}'.",
			}

		reg.status = "Rejected"
		reg.action_by = frappe.session.user or "Administrator"
		reg.action_at = frappe.utils.now_datetime()
		reg.flags.ignore_permissions = True
		reg.save()
		frappe.db.commit()

		return {
			"status": "success",
			"message": f"Sponsor application for '{reg.company_name}' ({reg.name}) has been rejected.",
			"registration": _format_sponsor_registration(reg),
		}

	else:
		reg.status = "Blocked"
		reg.action_by = frappe.session.user or "Administrator"
		reg.action_at = frappe.utils.now_datetime()
		reg.flags.ignore_permissions = True
		reg.save()

		blocked_ip_doc = None
		if reg.ip_address:
			blocked_ip_doc = block_ip_address(
				ip=reg.ip_address,
				reason=f"Blocked via sponsor application {reg.name} ({reg.company_name})",
				blocked_from=reg.name,
				blocked_by=frappe.session.user or "Administrator",
			)
		frappe.db.commit()

		return {
			"status": "success",
			"message": f"Sponsor application '{reg.name}' ({reg.company_name}) blocked. IP '{reg.ip_address}' is now blocked from submitting further requests.",
			"blocked_ip": reg.ip_address,
			"registration": _format_sponsor_registration(reg),
		}


def admin_accept_sponsor_registration(registration_id=None, **kwargs):
	"""Convenience wrapper for admin accept."""
	return admin_action_sponsor_registration(registration_id=registration_id, action="accept", **kwargs)


def admin_reject_sponsor_registration(registration_id=None, **kwargs):
	"""Convenience wrapper for admin reject."""
	return admin_action_sponsor_registration(registration_id=registration_id, action="reject", **kwargs)


def admin_block_sponsor_registration(registration_id=None, **kwargs):
	"""Convenience wrapper for admin block."""
	return admin_action_sponsor_registration(registration_id=registration_id, action="block", **kwargs)


@handle_api_errors("ConnectFest Admin Get Sponsor Registrations Error")
def admin_get_sponsor_registrations(status=None, search=None, page=1, limit=50, **kwargs):
	"""
	Admin API to view and filter all 'Become a Sponsor' registrations.
	"""
	err = check_admin_permission("sponsor registration management")
	if err:
		return err

	data = extract_request_data(kwargs)
	status_filter = (status or data.get("status") or "").strip()
	search_q = (search or data.get("search") or data.get("query") or "").strip().lower()

	raw_page = data.get("page") or page
	raw_limit = data.get("limit") or data.get("page_size") or limit or 50
	page_val = max(1, cint(raw_page))
	limit_val = min(200, max(1, cint(raw_limit)))
	offset_val = (page_val - 1) * limit_val

	conditions = ["1=1"]
	params = {}

	if status_filter and status_filter.lower() != "all":
		conditions.append("LOWER(status) = %(st)s")
		params["st"] = status_filter.lower()

	raw_block = data.get("block") if "block" in data else data.get("is_blocked")
	if raw_block is not None and str(raw_block).strip() != "":
		b_val = 1 if parse_boolean(raw_block) else 0
		if b_val == 1:
			conditions.append("(status = 'Blocked' OR ip_address IN (SELECT ip_address FROM `tabConnectFest Blocked IP` WHERE is_active = 1))")
		else:
			conditions.append("(status != 'Blocked' AND (ip_address IS NULL OR ip_address NOT IN (SELECT ip_address FROM `tabConnectFest Blocked IP` WHERE is_active = 1)))")

	if search_q:
		conditions.append("(LOWER(company_name) LIKE %(q)s OR LOWER(contact_person) LIKE %(q)s OR LOWER(email) LIKE %(q)s OR mobile_no LIKE %(q)s OR ip_address LIKE %(q)s)")
		params["q"] = f"%{search_q}%"

	where_clause = " AND ".join(conditions)

	total_filtered = frappe.db.sql(f"SELECT COUNT(*) FROM `tabConnectFest Sponsor Registration` WHERE {where_clause}", params)[0][0]
	total_all = frappe.db.sql("SELECT COUNT(*) FROM `tabConnectFest Sponsor Registration`")[0][0]
	pending_count = frappe.db.sql("SELECT COUNT(*) FROM `tabConnectFest Sponsor Registration` WHERE status = 'Pending'")[0][0]
	accepted_count = frappe.db.sql("SELECT COUNT(*) FROM `tabConnectFest Sponsor Registration` WHERE status = 'Accepted'")[0][0]
	rejected_count = frappe.db.sql("SELECT COUNT(*) FROM `tabConnectFest Sponsor Registration` WHERE status = 'Rejected'")[0][0]
	blocked_count = frappe.db.sql("SELECT COUNT(*) FROM `tabConnectFest Sponsor Registration` WHERE status = 'Blocked'")[0][0]

	rows = frappe.db.sql(f"""
		SELECT name, registration_id, company_name, contact_person, email, mobile_no,
		       logo, website_url, sponsor_category, sponsored_for, status,
		       ip_address, action_by, action_at, created_sponsor, creation, modified
		FROM `tabConnectFest Sponsor Registration`
		WHERE {where_clause}
		ORDER BY creation DESC
		LIMIT %(limit)s OFFSET %(offset)s
	""", {**params, "limit": limit_val, "offset": offset_val}, as_dict=True)

	formatted_list = [_format_sponsor_registration(r) for r in rows]

	return {
		"status": "success",
		"total_registrations": total_filtered,
		"summary": {
			"total": total_all,
			"pending": pending_count,
			"accepted": accepted_count,
			"rejected": rejected_count,
			"blocked": blocked_count,
		},
		"pagination": {
			"current_page": page_val,
			"page_size": limit_val,
			"total_items": total_filtered,
			"total_pages": (total_filtered + limit_val - 1) // limit_val if limit_val else 1,
			"has_next": (offset_val + limit_val) < total_filtered,
			"has_prev": page_val > 1,
		},
		"registrations": formatted_list,
	}


@handle_api_errors("ConnectFest Admin Get Sponsor Registration Details Error")
def admin_get_sponsor_registration_details(registration_id=None, name=None, **kwargs):
	"""
	Admin API to get details of a single sponsor application.
	"""
	err = check_admin_permission("sponsor registration management")
	if err:
		return err

	data = extract_request_data(kwargs)
	docname = (registration_id or name or data.get("registration_id") or data.get("name") or data.get("id") or "").strip()

	if not docname:
		frappe.local.response["http_status_code"] = 400
		return {"status": "error", "message": "registration_id is required"}

	if not frappe.db.exists("ConnectFest Sponsor Registration", docname):
		frappe.local.response["http_status_code"] = 404
		return {"status": "error", "message": f"Sponsor registration '{docname}' not found"}

	doc = frappe.get_doc("ConnectFest Sponsor Registration", docname)
	return {
		"status": "success",
		"registration": _format_sponsor_registration(doc),
	}


@handle_api_errors("ConnectFest Admin Get Blocked IPs Error")
def admin_get_blocked_ips(search=None, page=1, limit=50, **kwargs):
	"""
	Admin API to list all blocked IP addresses.
	"""
	err = check_admin_permission("blocked ip management")
	if err:
		return err

	data = extract_request_data(kwargs)
	search_q = (search or data.get("search") or data.get("query") or "").strip()
	raw_page = data.get("page") or page
	raw_limit = data.get("limit") or data.get("page_size") or limit or 50
	page_val = max(1, cint(raw_page))
	limit_val = min(200, max(1, cint(raw_limit)))
	offset_val = (page_val - 1) * limit_val

	conditions = ["is_active = 1"]
	params = {}

	if search_q:
		conditions.append("(ip_address LIKE %(q)s OR reason LIKE %(q)s OR blocked_from LIKE %(q)s)")
		params["q"] = f"%{search_q}%"

	where_clause = " AND ".join(conditions)
	total_count = frappe.db.sql(f"SELECT COUNT(*) FROM `tabConnectFest Blocked IP` WHERE {where_clause}", params)[0][0]

	rows = frappe.db.sql(f"""
		SELECT name, ip_address, is_active, reason, blocked_from, blocked_by, blocked_at, creation
		FROM `tabConnectFest Blocked IP`
		WHERE {where_clause}
		ORDER BY blocked_at DESC
		LIMIT %(limit)s OFFSET %(offset)s
	""", {**params, "limit": limit_val, "offset": offset_val}, as_dict=True)

	return {
		"status": "success",
		"total_blocked_ips": total_count,
		"blocked_ips": [
			{
				"id": r.name,
				"ip_address": r.ip_address,
				"reason": r.reason or "",
				"blocked_from": r.blocked_from or "",
				"blocked_by": r.blocked_by or "",
				"blocked_at": str(r.blocked_at or r.creation or ""),
			}
			for r in rows
		],
	}


@handle_api_errors("ConnectFest Admin Unblock IP Error")
def admin_unblock_ip(ip_address=None, ip=None, **kwargs):
	"""
	Admin API to unblock an IP address so it can submit requests again.
	"""
	err = check_admin_permission("blocked ip management")
	if err:
		return err

	data = extract_request_data(kwargs)
	target_ip = (ip_address or ip or data.get("ip_address") or data.get("ip") or "").strip()

	if not target_ip:
		frappe.local.response["http_status_code"] = 400
		return {"status": "error", "message": "ip_address is required"}

	unblock_ip_address(target_ip)

	return {
		"status": "success",
		"message": f"IP address '{target_ip}' has been successfully unblocked.",
		"unblocked_ip": target_ip,
	}


@handle_api_errors("ConnectFest Admin Block IP Error")
def admin_block_ip(ip_address=None, ip=None, reason=None, **kwargs):
	"""
	Admin API to manually block an IP address.
	"""
	err = check_admin_permission("blocked ip management")
	if err:
		return err

	data = extract_request_data(kwargs)
	target_ip = (ip_address or ip or data.get("ip_address") or data.get("ip") or "").strip()
	reason_val = (reason or data.get("reason") or "Manually blocked by Admin").strip()

	if not target_ip:
		frappe.local.response["http_status_code"] = 400
		return {"status": "error", "message": "ip_address is required"}

	block_ip_address(target_ip, reason=reason_val, blocked_by=frappe.session.user or "Administrator")

	return {
		"status": "success",
		"message": f"IP address '{target_ip}' has been successfully blocked.",
		"blocked_ip": target_ip,
	}
