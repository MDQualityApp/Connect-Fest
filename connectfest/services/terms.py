import json
import frappe
from frappe.utils import cint, now_datetime
from connectfest.services.common import (
	extract_request_data,
	handle_api_errors,
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


def _format_terms_doc(doc_or_dict):
	"""Standardizes Terms and Conditions output dictionary for API and frontend JSON consumption."""
	if isinstance(doc_or_dict, dict):
		name = doc_or_dict.get("name")
		title = doc_or_dict.get("title") or ""
		terms_type = doc_or_dict.get("terms_type") or "General"
		summary = doc_or_dict.get("summary") or ""
		content = doc_or_dict.get("content") or ""
		creation = str(doc_or_dict.get("creation") or "")
		modified = str(doc_or_dict.get("modified") or "")
	else:
		name = doc_or_dict.name
		title = doc_or_dict.title or ""
		terms_type = doc_or_dict.terms_type or "General"
		summary = doc_or_dict.summary or ""
		content = doc_or_dict.content or ""
		creation = str(doc_or_dict.creation)
		modified = str(doc_or_dict.modified)

	return {
		"id": name,
		"name": name,
		"title": title,
		"terms_type": terms_type,
		"summary": summary,
		"content": content,
		"created_at": creation,
		"updated_at": modified,
	}


# ==============================================================================
# 1. PUBLIC TERMS AND CONDITIONS APIS
# ==============================================================================

@handle_api_errors("ConnectFest Public Get Terms Error")
def get_terms_and_conditions(**kwargs):
	"""
	Public API to view Terms and Conditions.
	Clean and deduplicated response with filters support:
	- terms_type / type: 'Exhibitor', 'Visitor', 'Refund Policy', 'Privacy Policy', 'General', 'Other' (or 'all')
	- search / query: searches in title, summary, or content
	- terms_id / id / name: specific record ID (e.g. TC-01681)
	- page, limit: optional pagination
	"""
	data = extract_request_data(kwargs)
	terms_type = (data.get("terms_type") or data.get("type") or "").strip()
	search = (data.get("search") or data.get("query") or "").strip().lower()
	terms_id = (data.get("terms_id") or data.get("id") or data.get("name") or "").strip()

	raw_page = data.get("page")
	raw_limit = data.get("limit") or data.get("page_size")
	page = max(1, cint(raw_page)) if raw_page else 1
	limit = min(200, max(1, cint(raw_limit))) if raw_limit else 0
	offset = (page - 1) * limit if limit > 0 else 0

	conditions = ["1=1"]
	params = {}

	if terms_id:
		conditions.append("name = %(doc_id)s")
		params["doc_id"] = terms_id

	if terms_type and terms_type.lower() != "all":
		if terms_type.lower() in ("other", "others"):
			conditions.append("LOWER(terms_type) IN ('other', 'others')")
		else:
			conditions.append("LOWER(terms_type) = %(type)s")
			params["type"] = terms_type.lower()

	if search:
		conditions.append("(LOWER(title) LIKE %(q)s OR LOWER(content) LIKE %(q)s OR LOWER(summary) LIKE %(q)s)")
		params["q"] = f"%{search}%"

	where_clause = " AND ".join(conditions)
	limit_clause = f"LIMIT {limit} OFFSET {offset}" if limit > 0 else ""

	records = frappe.db.sql(
		f"""
		SELECT name, title, terms_type, summary, content, creation, modified
		FROM `tabConnectFest Terms and Conditions`
		WHERE {where_clause}
		ORDER BY creation ASC
		{limit_clause}
		""",
		params,
		as_dict=True,
	)

	formatted = [_format_terms_doc(r) for r in records]

	response = {
		"status": "success",
		"count": len(formatted),
		"filters_applied": {
			"terms_type": terms_type or "all",
			"search": search or None,
			"terms_id": terms_id or None,
		},
		"terms": formatted,
	}

	if limit > 0:
		response["page"] = page
		response["limit"] = limit

	return response


@handle_api_errors("ConnectFest Public Get Terms by Type Error")
def get_terms_by_type(terms_type=None, **kwargs):
	"""
	Public API shortcut for registration checkout flow.
	Fetches the Terms and Conditions for a specific type (e.g. Exhibitor, Visitor, General, Other).
	"""
	data = extract_request_data(kwargs)
	type_name = (terms_type or data.get("terms_type") or data.get("type") or "").strip()

	if not type_name:
		return {
			"status": "error",
			"message": "terms_type parameter is required (e.g., 'Exhibitor', 'Visitor', 'Refund Policy', 'General', 'Other').",
		}

	if type_name.lower() in ("other", "others"):
		where_type = "LOWER(terms_type) IN ('other', 'others')"
		params = {}
	else:
		where_type = "LOWER(terms_type) = %(type)s"
		params = {"type": type_name.lower()}

	record = frappe.db.sql(
		f"""
		SELECT name, title, terms_type, summary, content, creation, modified
		FROM `tabConnectFest Terms and Conditions`
		WHERE {where_type}
		ORDER BY creation DESC
		LIMIT 1
		""",
		params,
		as_dict=True,
	)

	if not record:
		return {
			"status": "error",
			"message": f"No terms and conditions found for type '{type_name}'.",
			"terms": None,
		}

	terms_doc = _format_terms_doc(record[0])
	return {
		"status": "success",
		"terms": terms_doc,
	}


# ==============================================================================
# 2. ADMIN TERMS AND CONDITIONS APIS
# ==============================================================================

@handle_api_errors("ConnectFest Admin Get Terms Error")
def admin_get_terms_and_conditions(**kwargs):
	"""
	Admin API: List all terms and conditions with filter and breakdown.
	"""
	err = check_admin_permission("terms and conditions")
	if err:
		return err
	data = extract_request_data(kwargs)

	terms_type = (data.get("terms_type") or data.get("type") or "").strip()
	search = (data.get("search") or data.get("query") or "").strip().lower()
	terms_id = (data.get("terms_id") or data.get("id") or data.get("name") or "").strip()

	raw_page = data.get("page")
	raw_limit = data.get("limit") or data.get("page_size")
	page = max(1, cint(raw_page)) if raw_page else 1
	limit = min(200, max(1, cint(raw_limit))) if raw_limit else 0
	offset = (page - 1) * limit if limit > 0 else 0

	conditions = ["1=1"]
	params = {}

	if terms_id:
		conditions.append("name = %(doc_id)s")
		params["doc_id"] = terms_id

	if terms_type and terms_type.lower() != "all":
		if terms_type.lower() in ("other", "others"):
			conditions.append("LOWER(terms_type) IN ('other', 'others')")
		else:
			conditions.append("LOWER(terms_type) = %(type)s")
			params["type"] = terms_type.lower()

	if search:
		conditions.append("(LOWER(title) LIKE %(q)s OR LOWER(content) LIKE %(q)s OR LOWER(summary) LIKE %(q)s)")
		params["q"] = f"%{search}%"

	where_clause = " AND ".join(conditions)

	total_count = frappe.db.sql(f"SELECT COUNT(*) FROM `tabConnectFest Terms and Conditions` WHERE {where_clause}", params)[0][0]

	limit_clause = f"LIMIT {limit} OFFSET {offset}" if limit > 0 else ""

	records = frappe.db.sql(
		f"""
		SELECT name, title, terms_type, summary, content, creation, modified
		FROM `tabConnectFest Terms and Conditions`
		WHERE {where_clause}
		ORDER BY creation ASC
		{limit_clause}
		""",
		params,
		as_dict=True,
	)

	formatted = [_format_terms_doc(r) for r in records]

	type_counts = frappe.db.sql(
		"""
		SELECT terms_type, COUNT(*) as cnt
		FROM `tabConnectFest Terms and Conditions`
		GROUP BY terms_type
		ORDER BY cnt DESC
		""",
		as_dict=True,
	)

	return {
		"status": "success",
		"total": total_count,
		"filters_applied": {
			"terms_type": terms_type or "all",
			"search": search or None,
			"terms_id": terms_id or None,
		},
		"type_breakdown": {tc["terms_type"]: tc["cnt"] for tc in type_counts},
		"page": page if limit > 0 else 1,
		"limit": limit if limit > 0 else total_count,
		"terms": formatted,
	}


@handle_api_errors("ConnectFest Admin Get Terms Item Error")
def admin_get_terms_item(terms_id=None, **kwargs):
	"""
	Admin API: Get single Terms and Conditions document.
	"""
	err = check_admin_permission("terms and conditions")
	if err:
		return err
	data = extract_request_data(kwargs)
	doc_id = terms_id or data.get("terms_id") or data.get("id") or data.get("name")

	if not doc_id:
		return {"status": "error", "message": "terms_id is required."}

	if not frappe.db.exists("ConnectFest Terms and Conditions", doc_id):
		return {"status": "error", "message": f"Terms and Conditions record '{doc_id}' not found."}

	doc = frappe.get_doc("ConnectFest Terms and Conditions", doc_id)
	return {
		"status": "success",
		"terms": _format_terms_doc(doc),
	}


@handle_api_errors("ConnectFest Admin Create Terms Error")
def admin_create_terms_and_conditions(**kwargs):
	"""
	Admin API: Create a new Terms and Conditions document.
	"""
	err = check_admin_permission("terms and conditions")
	if err:
		return err
	data = extract_request_data(kwargs)

	title = (data.get("title") or "").strip()
	terms_type = (data.get("terms_type") or data.get("type") or "General").strip()
	content = (data.get("content") or "").strip()

	if not title:
		return {"status": "error", "message": "Title is required for terms and conditions."}
	if not content:
		return {"status": "error", "message": "Content is required for terms and conditions."}

	# Normalize others to Other
	if terms_type.lower() in ("other", "others"):
		terms_type = "Other"

	summary = (data.get("summary") or "").strip()

	doc = frappe.new_doc("ConnectFest Terms and Conditions")
	doc.title = title
	doc.terms_type = terms_type
	doc.content = content
	doc.summary = summary
	doc.insert(ignore_permissions=True)
	frappe.db.commit()

	return {
		"status": "success",
		"message": f"Terms and Conditions '{doc.title}' created successfully.",
		"terms": _format_terms_doc(doc),
	}


@handle_api_errors("ConnectFest Admin Update Terms Error")
def admin_update_terms_and_conditions(terms_id=None, **kwargs):
	"""
	Admin API: Update an existing Terms and Conditions document.
	"""
	err = check_admin_permission("terms and conditions")
	if err:
		return err
	data = extract_request_data(kwargs)
	doc_id = terms_id or data.get("terms_id") or data.get("id") or data.get("name")

	if not doc_id:
		return {"status": "error", "message": "terms_id is required for update."}

	if not frappe.db.exists("ConnectFest Terms and Conditions", doc_id):
		return {"status": "error", "message": f"Terms and Conditions record '{doc_id}' not found."}

	doc = frappe.get_doc("ConnectFest Terms and Conditions", doc_id)

	if "title" in data:
		doc.title = str(data["title"]).strip()
	if "terms_type" in data:
		t = str(data["terms_type"]).strip()
		if t.lower() in ("other", "others"):
			t = "Other"
		doc.terms_type = t
	if "content" in data:
		doc.content = str(data["content"]).strip()
	if "summary" in data:
		doc.summary = str(data["summary"]).strip()

	doc.save(ignore_permissions=True)
	frappe.db.commit()

	return {
		"status": "success",
		"message": f"Terms and Conditions '{doc.title}' updated successfully.",
		"terms": _format_terms_doc(doc),
	}


@handle_api_errors("ConnectFest Admin Delete Terms Error")
def admin_delete_terms_and_conditions(terms_id=None, terms_ids=None, **kwargs):
	"""
	Admin API: Delete one or multiple Terms and Conditions documents.
	"""
	err = check_admin_permission("terms and conditions")
	if err:
		return err
	data = extract_request_data(kwargs)

	ids_to_delete = _extract_id_list(terms_ids or data.get("terms_ids") or terms_id or data.get("terms_id") or data.get("id"))

	if not ids_to_delete:
		return {"status": "error", "message": "No terms_id or terms_ids provided for deletion."}

	deleted = []
	failed = []

	for doc_id in ids_to_delete:
		try:
			if frappe.db.exists("ConnectFest Terms and Conditions", doc_id):
				frappe.delete_doc("ConnectFest Terms and Conditions", doc_id, ignore_permissions=True)
				deleted.append(doc_id)
			else:
				failed.append({"id": doc_id, "error": "Not found"})
		except Exception as e:
			failed.append({"id": doc_id, "error": str(e)})

	frappe.db.commit()

	return {
		"status": "success",
		"message": f"Successfully deleted {len(deleted)} terms document(s).",
		"deleted_count": len(deleted),
		"deleted_ids": deleted,
		"failed": failed,
	}


# ==============================================================================
# 3. SEED DEFAULT TERMS AND CONDITIONS
# ==============================================================================

@handle_api_errors("ConnectFest Seed Default Terms Error")
def seed_default_terms(**kwargs):
	"""
	Seeds realistic default Terms and Conditions for ConnectFest 2027.
	Safe to run multiple times: updates existing terms or creates if missing.
	"""
	default_terms_list = [
		{
			"title": "Exhibitor Stall Agreement & Guidelines 2027",
			"terms_type": "Exhibitor",
			"summary": "Mandatory stall allocation, payment milestones, setup timings, and booth decor regulations for all ConnectFest exhibitors.",
			"content": """### ConnectFest 2027 - Exhibitor Terms & Conditions

1. **Stall Allocation & Usage**
   - Stalls are allocated on a confirmed payment basis.
   - Sub-letting, sharing, or transferring the allotted stall to another entity without prior written permission from the ConnectFest Organizing Committee is strictly prohibited.
   - Standard stall inclusions: Octanorm shell scheme booth, 1 table, 2 chairs, 3 spot lights, 1 power socket (5A/230V), and company fascia name board.

2. **Setup and Dismantling Timelines**
   - Stall fabrication and decoration can commence from 2:00 PM on the day preceding the event.
   - All stalls must be fully setup and ready by 8:00 AM on the morning of Day 1.
   - Stall dismantling must be completed within 4 hours after the official closing ceremony on the final event day.

3. **Power and Audio Regulations**
   - Standard 5A single-phase power is provided for standard demo equipment. Heavy industrial loads (above 1KW) require prior approval and additional electrical charges.
   - Sound and audio-visual demos at individual stalls must not exceed 65 dB to prevent disruption to neighboring exhibitors.

4. **Safety & Security**
   - Exhibitors are responsible for the safety of their goods, sample products, and valuables. 24x7 venue security is provided, but ConnectFest is not liable for lost or damaged personal items.
   - Fire safety protocols must be observed. Flammable materials and open flames are strictly forbidden inside the expo hall.

5. **Compliance & Code of Conduct**
   - Exhibitors must uphold professional ethics and treat all attendees and fellow exhibitors with courtesy and respect.
   - Distribution of marketing pamphlets outside the designated stall perimeter or in common corridors is prohibited.""",
		},
		{
			"title": "Visitor Entry & Pass Policy 2027",
			"terms_type": "Visitor",
			"summary": "Digital QR gate pass terms, badge display rules, attendee safety, and networking decorum for ConnectFest visitors.",
			"content": """### ConnectFest 2027 - Visitor Entry & Pass Terms

1. **Digital Gate Pass & Verification**
   - Each registered visitor is issued a unique Digital QR Gate Pass.
   - The QR pass must be presented at the venue entry turnstiles for gate scan verification.
   - Badges must be worn and clearly visible at all times within the exhibition venue and conference halls.

2. **Non-Transferable Entry**
   - Visitor passes are non-transferable and valid exclusively for the registered individual named on the pass.
   - Photo ID proof (Aadhaar, Driving License, or Company ID) may be requested at security checkpoints.

3. **Attendee Decorum & Networking**
   - ConnectFest is a premier business networking and trade exhibition. Visitors are expected to maintain professional conduct.
   - Aggressive sales pitching, unauthorized flyer distribution, or harassment of exhibitors or attendees will result in immediate badge revocation and escort from the venue.

4. **Photography and Media Consent**
   - ConnectFest organizers and official media partners will photograph and record video coverage throughout the event.
   - By entering the venue, attendees grant permission for their likeness to appear in post-event highlights, website galleries, and promotional media.

5. **Prohibited Items**
   - Hazardous materials, weapons, outside alcoholic beverages, and illegal substances are strictly prohibited inside the premises.""",
		},
		{
			"title": "Refund, Cancellation & Force Majeure Policy",
			"terms_type": "Refund Policy",
			"summary": "Cancellation timelines, refund deduction slabs, processing windows, and force majeure event contingency terms.",
			"content": """### ConnectFest 2027 - Refund & Cancellation Policy

1. **Exhibitor Stall Cancellation Slabs**
   - **More than 45 days prior to event date:** 75% refund of total stall fee (25% retained as administrative and reservation fee).
   - **30 to 45 days prior to event date:** 50% refund of total stall fee.
   - **Less than 30 days prior to event date:** Non-refundable, as stalls cannot be re-allocated on short notice.

2. **Visitor Pass Cancellation**
   - Visitor passes can be cancelled up to 7 days before the event date for a full refund minus payment gateway transaction charges (typically 2-3%).
   - Passes cancelled within 7 days of the event or no-shows are non-refundable.

3. **Refund Processing Mode & Timeline**
   - Approved refunds will be credited back to the original payment source (Credit/Debit Card, UPI, Net Banking) within 7-10 business days.
   - For cash or offline payments booked via Admin, refunds will be issued by NEFT/RTGS bank transfer to the registered corporate bank account.

4. **Event Postponement or Force Majeure**
   - In the unforeseen event of postponement, government restrictions, severe weather, or force majeure events beyond reasonable control, booked stalls and passes will automatically roll over to the rescheduled dates.
   - If the event is permanently cancelled by the organizers without a reschedule date, 100% of stall and pass fees will be refunded.""",
		},
		{
			"title": "Privacy Policy & Attendee Data Usage",
			"terms_type": "Privacy Policy",
			"summary": "How contact details, business cards, QR badge scans, and exhibitor listings are safely handled under data protection standards.",
			"content": """### ConnectFest 2027 - Privacy & Data Protection Policy

1. **Information Collection**
   - ConnectFest collects attendee and exhibitor information (Full Name, Email, Mobile Number, Company Name, Industry Segment, BNI Chapter affiliation) during registration to issue credentials and facilitate networking.

2. **Digital QR Badge Scans**
   - When an attendee voluntarily allows an exhibitor or fellow attendee to scan their digital pass QR code, they consent to sharing their business contact details with that exhibitor for professional networking follow-up.

3. **Public Directory Listing**
   - Confirmed exhibitor company names, stall numbers, and industry categories are published in the ConnectFest Event Guide and Online Exhibitor Directory.
   - Personal mobile numbers and direct private emails are never publicly disclosed without explicit consent.

4. **Security & Data Retention**
   - All transaction data is processed over secure 256-bit SSL encryption. We do not store sensitive payment card details or CVVs on our servers.
   - Attendees may request modification or erasure of their account records after the event by contacting privacy@connectfest.in.""",
		},
		{
			"title": "General Code of Conduct & Safety Guidelines",
			"terms_type": "Other",
			"summary": "Harassment-free environment commitment, safety protocols, emergency exits, and medical assistance details.",
			"content": """### ConnectFest 2027 - General Code of Conduct & Venue Safety

1. **Harassment-Free Event Commitment**
   - ConnectFest is committed to providing a welcoming, inclusive, and harassment-free event experience for everyone regardless of gender, sexual orientation, disability, physical appearance, race, or religion.
   - Harassment in any form, including inappropriate physical contact, unwelcome sexual attention, or intimidation, will not be tolerated.

2. **Venue Safety & Emergency Procedures**
   - Emergency exits and evacuation floor maps are clearly marked throughout the venue.
   - Keep all gangways and main aisles clear of boxes, displays, and promotional rollups.
   - In case of medical emergency, a fully-equipped First Aid Room with trained medical paramedics and an on-site ambulance is stationed near Gate 2.

3. **Lost & Found**
   - A dedicated Lost & Found desk is situated at the Central Helpdesk in Hall A. Items not claimed within 14 days will be surrendered to venue security.""",
		},
	]

	seeded = []
	for item in default_terms_list:
		existing = frappe.db.sql(
			"SELECT name FROM `tabConnectFest Terms and Conditions` WHERE terms_type = %(type)s LIMIT 1",
			{"type": item["terms_type"]},
			as_dict=True,
		)

		if existing:
			doc = frappe.get_doc("ConnectFest Terms and Conditions", existing[0]["name"])
			doc.title = item["title"]
			doc.summary = item["summary"]
			doc.content = item["content"]
			doc.save(ignore_permissions=True)
			seeded.append({"action": "updated", "id": doc.name, "title": doc.title, "type": doc.terms_type})
		else:
			doc = frappe.new_doc("ConnectFest Terms and Conditions")
			doc.title = item["title"]
			doc.terms_type = item["terms_type"]
			doc.summary = item["summary"]
			doc.content = item["content"]
			doc.insert(ignore_permissions=True)
			seeded.append({"action": "created", "id": doc.name, "title": doc.title, "type": doc.terms_type})

	frappe.db.commit()

	return {
		"status": "success",
		"message": f"Successfully seeded {len(seeded)} default Terms and Conditions documents.",
		"seeded": seeded,
	}
