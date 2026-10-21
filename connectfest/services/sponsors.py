import json
import uuid
import frappe
from frappe.utils import cint, now_datetime
from connectfest.services.common import (
	extract_request_data,
	parse_boolean,
	handle_api_errors,
	save_image_from_base64_or_url,
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
		description = doc_or_dict.description or ""
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
		"logo": logo,
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

	return {
		"status": "success",
		"total_sponsors": total_count,
		"featured_count": featured_count,
		"categories": available_categories,
		"active_category": category or "All",
		"sponsors": formatted_sponsors,
	}


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
		else (data.get("order") if "order" in data else (display_order if display_order is not None else 0))
	)
	s_order = cint(raw_order)

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
	if description is not None or "description" in data:
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


def seed_default_sponsors():
	"""
	Seeds default realistic sponsors for ConnectFest 2027 if none exist.
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
	print("ConnectFest: Seeded 5 official ConnectFest 2027 sponsors.")
