import io
import csv
import frappe
from frappe.utils import now_datetime, flt, cint
from connectfest.services.common import extract_request_data, handle_api_errors, parse_boolean, get_base_url
from connectfest.services.auth import check_admin_permission


def fetch_report_data(filters=None):
	"""
	Universal fetcher for exhibitor bookings and visitor passes with comprehensive filters.
	"""
	filters = filters or {}
	raw_role = filters.get("role") or filters.get("user_type") or "all"
	role = (raw_role if isinstance(raw_role, str) else "all").strip().lower()

	raw_status = filters.get("status") or ""
	status_filter = (raw_status if isinstance(raw_status, str) else "").strip().lower()

	raw_pay_status = filters.get("payment_status") or ""
	payment_status = (raw_pay_status if isinstance(raw_pay_status, str) else "").strip()

	raw_pay_mode = filters.get("payment_mode") or ""
	payment_mode = (raw_pay_mode if isinstance(raw_pay_mode, str) else "").strip().lower()

	bni_filter = filters.get("is_bni_member") if filters.get("is_bni_member") is not None else filters.get("bni")

	raw_tier = filters.get("tier") or filters.get("stall_category") or ""
	tier_filter = (raw_tier if isinstance(raw_tier, str) else "").strip().lower()

	raw_search = filters.get("search") or filters.get("query") or ""
	search_query = (raw_search if isinstance(raw_search, str) else "").strip().lower()

	rows = []

	# 1. Fetch Exhibitors (if role is all or exhibitor)
	if role in ("all", "exhibitor", "exhibitors"):
		ex_conds = ["1=1"]
		ex_params = {}
		if payment_status and payment_status.lower() != "all":
			ex_conds.append("LOWER(payment_status) = %(p_status)s")
			ex_params["p_status"] = payment_status.lower()
		if bni_filter is not None and str(bni_filter).strip() not in ("", "all"):
			target_bni = 1 if str(bni_filter).strip().lower() in ("1", "true", "yes") else 0
			ex_conds.append("is_bni_member = %(bni)s")
			ex_params["bni"] = target_bni
		if tier_filter and tier_filter != "all":
			ex_conds.append("LOWER(stall_category) = %(tier)s")
			ex_params["tier"] = tier_filter.lower()
		if status_filter and status_filter != "all":
			ex_conds.append("LOWER(status) = %(st_filter)s")
			ex_params["st_filter"] = status_filter.lower()
		if search_query:
			ex_conds.append("(LOWER(registration_id) LIKE %(q)s OR LOWER(full_name) LIKE %(q)s OR LOWER(company_name) LIKE %(q)s OR LOWER(email) LIKE %(q)s OR mobile_no LIKE %(q)s OR LOWER(stall_number) LIKE %(q)s)")
			ex_params["q"] = f"%{search_query}%"

		ex_where = " AND ".join(ex_conds)
		ex_query = f"""
			SELECT name, registration_id, full_name, email, mobile_no, company_name,
			       location, is_bni_member, region_name, chapter_name, industry, segment,
			       source_of_info, stall_number, stall_category, amount, payment_status,
			       payment_mode, razorpay_payment_id, payment_date, status, creation,
			       bill, gst
			FROM `tabConnectFest Registration`
			WHERE {ex_where}
			ORDER BY creation DESC
		"""
		ex_records = frappe.db.sql(ex_query, ex_params, as_dict=True)

		for r in ex_records:
			pay_mode = r.get("payment_mode") or ("Cash" if (r.get("razorpay_payment_id") or "").startswith("CASH") else ("Online" if r.get("payment_status") == "Paid" else "N/A"))
			is_bni = bool(r.get("is_bni_member"))

			if payment_mode and payment_mode != "all":
				if pay_mode.lower() != payment_mode:
					continue

			rows.append({
				"registration_id": r.get("registration_id") or r.get("name"),
				"user_type": "Exhibitor",
				"full_name": r.get("full_name") or "",
				"email": r.get("email") or "",
				"mobile_no": r.get("mobile_no") or "",
				"company_name": r.get("company_name") or "",
				"location": r.get("location") or "",
				"bill": r.get("bill") or "-",
				"gst": r.get("gst") or "-",
				"is_bni_member": "Yes" if is_bni else "No",
				"region_name": r.get("region_name") or "-",
				"chapter_name": r.get("chapter_name") or "-",
				"industry": r.get("industry") or "-",
				"segment": r.get("segment") or "-",
				"stall_number": r.get("stall_number") or "-",
				"stall_category": r.get("stall_category") or "-",
				"amount": flt(r.get("amount", 0.0)),
				"payment_status": r.get("payment_status") or "Pending",
				"payment_mode": pay_mode,
				"receipt_id": r.get("razorpay_payment_id") or "-",
				"payment_date": str(r.get("payment_date") or "-"),
				"source_of_info": r.get("source_of_info") or "-",
				"status": r.get("status") or "Registered",
				"creation": str(r.get("creation") or "")[:19],
			})

	# 2. Fetch Visitors (if role is all or visitor, and tier filter not specified)
	if role in ("all", "visitor", "visitors") and (not tier_filter or tier_filter == "all"):
		vis_conds = ["1=1"]
		vis_params = {}
		if status_filter and status_filter != "all":
			vis_conds.append("LOWER(status) = %(st_filter)s")
			vis_params["st_filter"] = status_filter.lower()
		if search_query:
			vis_conds.append("(LOWER(registration_id) LIKE %(q)s OR LOWER(full_name) LIKE %(q)s OR LOWER(email) LIKE %(q)s OR mobile_no LIKE %(q)s OR LOWER(pass_code) LIKE %(q)s)")
			vis_params["q"] = f"%{search_query}%"

		vis_where = " AND ".join(vis_conds)
		vis_query = f"""
			SELECT name, registration_id, full_name, visitor_image, email, mobile_no, which_day,
			       pass_code, source_of_info, checked_in, checked_in_at, status, creation,
			       area_of_interest
			FROM `tabConnectFest Visitor Registration`
			WHERE {vis_where}
			ORDER BY creation DESC
		"""
		vis_records = frappe.db.sql(vis_query, vis_params, as_dict=True)

		for v in vis_records:
			if payment_status and payment_status.lower() not in ("all", "free", "paid"):
				continue
			if payment_mode and payment_mode not in ("all", "free"):
				continue
			if bni_filter is not None and str(bni_filter).strip() not in ("", "all"):
				if str(bni_filter).strip().lower() in ("1", "true", "yes"):
					continue
				continue
			if payment_mode and payment_mode not in ("all", "free"):
				continue
			if bni_filter is not None and str(bni_filter).strip() not in ("", "all"):
				if str(bni_filter).strip().lower() in ("1", "true", "yes"):
					continue

			if status_filter and status_filter != "all":
				if (v.get("status") or "").lower() != status_filter:
					continue

			if search_query:
				q = search_query
				combined = " ".join([
					str(v.get("registration_id") or ""),
					str(v.get("full_name") or ""),
					str(v.get("email") or ""),
					str(v.get("mobile_no") or ""),
					str(v.get("pass_code") or ""),
					str(v.get("source_of_info") or ""),
					"visitor free entry",
				]).lower()
				if q not in combined:
					continue

			rows.append({
				"registration_id": v.get("registration_id") or v.get("name"),
				"user_type": "Visitor",
				"full_name": v.get("full_name") or "",
				"visitor_image": v.get("visitor_image") or "",
				"email": v.get("email") or "",
				"mobile_no": v.get("mobile_no") or "",
				"company_name": "-",
				"location": "-",
				"is_bni_member": "No",
				"region_name": "-",
				"chapter_name": "-",
				"industry": "-",
				"segment": "-",
				"stall_number": "-",
				"stall_category": f"Visitor Pass ({v.get('which_day') or 'Both days'})",
				"amount": 0.0,
				"payment_status": "Free Entry",
				"payment_mode": "Free",
				"receipt_id": v.get("pass_code") or "-",
				"payment_date": "-",
				"source_of_info": v.get("source_of_info") or "-",
				"area_of_interest": v.get("area_of_interest") or "-",
				"status": "Checked In" if v.get("checked_in") else (v.get("status") or "Registered"),
				"creation": str(v.get("creation") or "")[:19],
			})

	return rows


def generate_csv_string(rows):
	"""Generates standard CSV with UTF-8 BOM for direct Microsoft Excel compatibility."""
	output = io.StringIO()
	# Write UTF-8 BOM
	output.write("\ufeff")

	fieldnames = [
		"Registration ID",
		"Attendee Name",
		"Email Address",
		"Mobile Number",
		"Role",
		"Company Name",
		"Location",
		"BNI Member",
		"Region Name",
		"Chapter Name",
		"Industry",
		"Segment",
		"Stall Number",
		"Stall Tier",
		"Amount (INR)",
		"Payment Status",
		"Payment Mode",
		"Payment / Receipt ID",
		"Payment Date",
		"Source of Info",
		"Status",
		"Registration Date",
	]

	writer = csv.DictWriter(output, fieldnames=fieldnames)
	writer.writeheader()

	for r in rows:
		writer.writerow({
			"Registration ID": r["registration_id"],
			"Attendee Name": r["full_name"],
			"Email Address": r["email"],
			"Mobile Number": r["mobile_no"],
			"Role": r["user_type"],
			"Company Name": r["company_name"],
			"Location": r["location"],
			"BNI Member": r["is_bni_member"],
			"Region Name": r["region_name"],
			"Chapter Name": r["chapter_name"],
			"Industry": r["industry"],
			"Segment": r["segment"],
			"Stall Number": r["stall_number"],
			"Stall Tier": r["stall_category"],
			"Amount (INR)": f"{r['amount']:.2f}",
			"Payment Status": r["payment_status"],
			"Payment Mode": r["payment_mode"],
			"Payment / Receipt ID": r["receipt_id"],
			"Payment Date": r["payment_date"],
			"Source of Info": r["source_of_info"],
			"Status": r["status"],
			"Registration Date": r["creation"],
		})

	return output.getvalue()


def generate_pdf_html(rows, filters_applied=None):
	"""Constructs a responsive, beautifully styled HTML document for WeasyPrint PDF conversion."""
	filters_applied = filters_applied or {}
	now_str = now_datetime().strftime("%d %b %Y, %I:%M %p")

	total_records = len(rows)
	total_exhibitors = sum(1 for r in rows if r["user_type"] == "Exhibitor")
	total_visitors = sum(1 for r in rows if r["user_type"] == "Visitor")
	total_bni = sum(1 for r in rows if r["is_bni_member"] == "Yes")
	total_revenue = sum(r["amount"] for r in rows if r["payment_status"] == "Paid")
	cash_revenue = sum(r["amount"] for r in rows if r["payment_status"] == "Paid" and r["payment_mode"].lower() == "cash")
	online_revenue = sum(r["amount"] for r in rows if r["payment_status"] == "Paid" and r["payment_mode"].lower() == "online")

	table_rows_html = ""
	for idx, r in enumerate(rows, 1):
		bg = "#ffffff" if idx % 2 != 0 else "#f8fafc"
		pay_badge_color = "#16a34a" if r["payment_status"] == "Paid" else ("#ca8a04" if r["payment_status"] == "Pending" else "#64748b")
		bni_badge = "<span style='color:#dc2626; font-weight:700;'>BNI</span>" if r["is_bni_member"] == "Yes" else "<span style='color:#94a3b8;'>-</span>"

		table_rows_html += f"""
		<tr style="background: {bg}; border-bottom: 1px solid #e2e8f0; font-size: 10px;">
			<td style="padding: 6px 4px; font-weight: 600; font-family: monospace;">{r['registration_id']}</td>
			<td style="padding: 6px 4px;"><strong>{r['full_name']}</strong><br><span style="color:#64748b; font-size:9px;">{r['email']}</span></td>
			<td style="padding: 6px 4px;">{r['company_name']}<br><span style="color:#64748b; font-size:9px;">{r['location']}</span></td>
			<td style="padding: 6px 4px; text-align: center;">{bni_badge}<br><span style="color:#64748b; font-size:8px;">{r['chapter_name']}</span></td>
			<td style="padding: 6px 4px; font-weight: 700; color: #4f46e5; font-size: 11px;">{r['stall_number']}</td>
			<td style="padding: 6px 4px;">{r['stall_category']}</td>
			<td style="padding: 6px 4px; font-weight: 700; text-align: right;">₹{r['amount']:,.0f}</td>
			<td style="padding: 6px 4px; text-align: center;">
				<span style="display:inline-block; padding: 2px 6px; border-radius: 4px; font-size: 9px; font-weight: 600; color: {pay_badge_color}; background: #f1f5f9;">
					{r['payment_status']}
				</span>
			</td>
			<td style="padding: 6px 4px; text-align: center; font-weight: 600;">{r['payment_mode']}</td>
			<td style="padding: 6px 4px; color: #64748b; font-size: 9px;">{r['creation'][:10]}</td>
		</tr>
		"""

	return f"""
	<!DOCTYPE html>
	<html>
	<head>
		<meta charset="utf-8">
		<title>ConnectFest 2027 Bookings Report</title>
		<style>
			@page {{
				size: A4 landscape;
				margin: 12mm 10mm;
				@bottom-right {{
					content: "Page " counter(page) " of " counter(pages);
					font-size: 9px;
					color: #94a3b8;
				}}
				@bottom-left {{
					content: "ConnectFest 2027 • Official Bookings Report";
					font-size: 9px;
					color: #94a3b8;
				}}
			}}
			body {{
				font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
				color: #0f172a;
				margin: 0;
				padding: 0;
			}}
			.header {{
				border-bottom: 2px solid #4f46e5;
				padding-bottom: 12px;
				margin-bottom: 16px;
			}}
			.summary-box {{
				display: flex;
				margin-bottom: 16px;
				gap: 12px;
			}}
			.kpi-card {{
				background: #f8fafc;
				border: 1px solid #e2e8f0;
				border-radius: 6px;
				padding: 8px 12px;
				min-width: 110px;
			}}
			.kpi-label {{
				font-size: 9px;
				text-transform: uppercase;
				letter-spacing: 0.5px;
				color: #64748b;
				margin-bottom: 2px;
				font-weight: 600;
			}}
			.kpi-val {{
				font-size: 16px;
				font-weight: 800;
				color: #1e293b;
			}}
			table {{
				width: 100%;
				border-collapse: collapse;
			}}
			th {{
				background: #1e293b;
				color: #ffffff;
				font-size: 10px;
				font-weight: 600;
				text-align: left;
				padding: 8px 4px;
				text-transform: uppercase;
				letter-spacing: 0.5px;
			}}
		</style>
	</head>
	<body>
		<div class="header">
			<table style="width: 100%;">
				<tr>
					<td>
						<div style="font-size: 11px; font-weight: 700; color: #4f46e5; letter-spacing: 1.5px; text-transform: uppercase;">ConnectFest 2027 • Chennai Trade Centre</div>
						<h1 style="margin: 2px 0 0; font-size: 20px; font-weight: 800; color: #0f172a;">Attendee & Stall Bookings Audit Report</h1>
					</td>
					<td style="text-align: right; vertical-align: bottom;">
						<div style="font-size: 10px; color: #64748b;">Generated On: <strong>{now_str}</strong></div>
						<div style="font-size: 10px; color: #64748b;">Filter Scope: <strong>{filters_applied.get('role', 'All Attendees').capitalize()}</strong></div>
					</td>
				</tr>
			</table>
		</div>

		<table style="width: 100%; margin-bottom: 16px;">
			<tr>
				<td style="width: 16%;">
					<div class="kpi-card">
						<div class="kpi-label">Total Records</div>
						<div class="kpi-val">{total_records}</div>
					</div>
				</td>
				<td style="width: 16%;">
					<div class="kpi-card">
						<div class="kpi-label">Exhibitors</div>
						<div class="kpi-val" style="color: #4f46e5;">{total_exhibitors}</div>
					</div>
				</td>
				<td style="width: 16%;">
					<div class="kpi-card">
						<div class="kpi-label">BNI Members</div>
						<div class="kpi-val" style="color: #dc2626;">{total_bni}</div>
					</div>
				</td>
				<td style="width: 16%;">
					<div class="kpi-card">
						<div class="kpi-label">Total Paid Revenue</div>
						<div class="kpi-val" style="color: #16a34a;">₹{total_revenue:,.0f}</div>
					</div>
				</td>
				<td style="width: 18%;">
					<div class="kpi-card">
						<div class="kpi-label">Cash Collections</div>
						<div class="kpi-val" style="color: #0891b2;">₹{cash_revenue:,.0f}</div>
					</div>
				</td>
				<td style="width: 18%;">
					<div class="kpi-card">
						<div class="kpi-label">Online Collections</div>
						<div class="kpi-val" style="color: #7c3aed;">₹{online_revenue:,.0f}</div>
					</div>
				</td>
			</tr>
		</table>

		<table>
			<thead>
				<tr>
					<th style="width: 11%;">Reg ID</th>
					<th style="width: 17%;">Name & Email</th>
					<th style="width: 16%;">Company & City</th>
					<th style="width: 11%; text-align: center;">BNI Chapter</th>
					<th style="width: 8%;">Stall</th>
					<th style="width: 10%;">Category</th>
					<th style="width: 9%; text-align: right;">Amount</th>
					<th style="width: 8%; text-align: center;">Payment</th>
					<th style="width: 6%; text-align: center;">Mode</th>
					<th style="width: 8%;">Date</th>
				</tr>
			</thead>
			<tbody>
				{table_rows_html if table_rows_html else '<tr><td colspan="10" style="text-align:center; padding:24px; color:#94a3b8;">No records found matching the active filters</td></tr>'}
			</tbody>
		</table>
	</body>
	</html>
	"""


@handle_api_errors("ConnectFest Admin Export Bookings CSV Error")
def admin_export_bookings_csv(**kwargs):
	"""
	Admin API to download a filtered CSV report of all attendee and stall bookings.
	"""
	err = check_admin_permission("export bookings report")
	if err:
		return err

	data = extract_request_data(kwargs)
	rows = fetch_report_data(data)
	csv_content = generate_csv_string(rows)

	now_tag = now_datetime().strftime("%Y%m%d_%H%M%S")
	filename = f"connectfest_bookings_{now_tag}.csv"

	frappe.local.response.filename = filename
	frappe.local.response.filecontent = csv_content.encode("utf-8-sig")
	frappe.local.response.type = "download"
	frappe.local.response.content_type = "text/csv; charset=utf-8"


@handle_api_errors("ConnectFest Admin Export Bookings PDF Error")
def admin_export_bookings_pdf(**kwargs):
	"""
	Admin API to download a filtered PDF report using WeasyPrint.
	"""
	err = check_admin_permission("export bookings report")
	if err:
		return err

	data = extract_request_data(kwargs)
	rows = fetch_report_data(data)
	html_content = generate_pdf_html(rows, filters_applied=data)

	now_tag = now_datetime().strftime("%Y%m%d_%H%M%S")
	filename = f"connectfest_bookings_{now_tag}.pdf"

	try:
		import weasyprint
		pdf_bytes = weasyprint.HTML(string=html_content).write_pdf()

		frappe.local.response.filename = filename
		frappe.local.response.filecontent = pdf_bytes
		frappe.local.response.type = "download"
		frappe.local.response.content_type = "application/pdf"
	except Exception as e:
		frappe.log_error(title="WeasyPrint PDF Generation Error", message=frappe.get_traceback())
		# Graceful fallback: return printable HTML if PDF rendering fails
		frappe.local.response.filecontent = html_content
		frappe.local.response.type = "page"
		frappe.local.response.content_type = "text/html; charset=utf-8"


@handle_api_errors("ConnectFest Admin Get Bookings Report Error")
def admin_get_bookings_report(**kwargs):
	"""
	Admin API returning structured JSON report + summary metrics + download links.
	"""
	err = check_admin_permission("bookings report")
	if err:
		return err

	data = extract_request_data(kwargs)
	rows = fetch_report_data(data)

	total_records = len(rows)
	total_exhibitors = sum(1 for r in rows if r["user_type"] == "Exhibitor")
	total_visitors = sum(1 for r in rows if r["user_type"] == "Visitor")
	total_bni = sum(1 for r in rows if r["is_bni_member"] == "Yes")
	total_revenue = sum(r["amount"] for r in rows if r["payment_status"] == "Paid")
	cash_revenue = sum(r["amount"] for r in rows if r["payment_status"] == "Paid" and r["payment_mode"].lower() == "cash")
	online_revenue = sum(r["amount"] for r in rows if r["payment_status"] == "Paid" and r["payment_mode"].lower() == "online")

	query_params = []
	for k in ["role", "status", "payment_status", "payment_mode", "is_bni_member", "tier", "search"]:
		if data.get(k): query_params.append(f"{k}={data[k]}")
	q_str = ("?" + "&".join(query_params)) if query_params else ""

	base_url = get_base_url()

	return {
		"status": "success",
		"total_records": total_records,
		"summary": {
			"total_attendees": total_records,
			"total_exhibitors": total_exhibitors,
			"total_visitors": total_visitors,
			"total_bni_members": total_bni,
			"total_revenue": total_revenue,
			"cash_revenue": cash_revenue,
			"online_revenue": online_revenue,
		},
		"downloads": {
			"csv_url": f"{base_url}/api/method/connectfest.api.admin_export_bookings_csv{q_str}",
			"pdf_url": f"{base_url}/api/method/connectfest.api.admin_export_bookings_pdf{q_str}",
		},
		"filters_applied": data,
		"records": rows,
	}


@handle_api_errors("ConnectFest Admin Export Error")
def admin_export(**kwargs):
	"""
	Unified Export API for ConnectFest Bookings & Attendees.
	Payload fields:
	- format: 'csv' | 'pdf' | 'json' (default: 'json')
	- download: 1 or 0 (if 1 and format is csv/pdf, directly triggers file download)
	- Filters: role, payment_status, payment_mode, is_bni_member, tier, status, search
	"""
	err = check_admin_permission("export bookings report")
	if err:
		return err

	data = extract_request_data(kwargs)
	raw_fmt = data.get("format") or data.get("export_format")
	if not raw_fmt and isinstance(data.get("type"), str) and data.get("type").strip().lower() in ("csv", "pdf", "json"):
		raw_fmt = data.get("type")
	elif isinstance(raw_fmt, dict):
		raw_fmt = raw_fmt.get("format") or raw_fmt.get("export_format") or raw_fmt.get("type")
	if not isinstance(raw_fmt, str):
		raw_fmt = "json"
	fmt = raw_fmt.strip().lower()
	if fmt not in ("csv", "pdf", "json"):
		fmt = "json"

	download = parse_boolean(data.get("download", 0))

	query_params = []
	for k in ["role", "status", "payment_status", "payment_mode", "is_bni_member", "tier", "search"]:
		val = data.get(k)
		if val is not None and not isinstance(val, (dict, list)) and str(val).strip() != "":
			query_params.append(f"{k}={val}")
	q_str = ("?" + "&".join(query_params)) if query_params else ""

	now_tag = now_datetime().strftime("%Y%m%d_%H%M%S")
	base_url = get_base_url()

	if fmt == "csv":
		if download:
			return admin_export_bookings_csv(**kwargs)
		rows = fetch_report_data(data)
		return {
			"status": "success",
			"format": "csv",
			"download_url": f"{base_url}/api/method/connectfest.api.admin_export_bookings_csv{q_str}",
			"filename": f"connectfest_bookings_{now_tag}.csv",
			"total_records": len(rows),
			"filters_applied": data,
		}

	elif fmt == "pdf":
		if download:
			return admin_export_bookings_pdf(**kwargs)
		rows = fetch_report_data(data)
		return {
			"status": "success",
			"format": "pdf",
			"download_url": f"{base_url}/api/method/connectfest.api.admin_export_bookings_pdf{q_str}",
			"filename": f"connectfest_bookings_{now_tag}.pdf",
			"total_records": len(rows),
			"filters_applied": data,
		}

	else:
		# Default format: JSON report
		return admin_get_bookings_report(**kwargs)


@handle_api_errors("ConnectFest Download Stall Allocation Letter Error")
def download_stall_allocation_letter(**kwargs):
	"""
	Generates and downloads the official 3-page Stall Allocation Letter & Sales Invoice (PDF)
	in the official ConnectFest 2027 template format.
	Accessible by registration_id, email, or mobile_no.
	"""
	from connectfest.services.invoice import get_registration_allocation_data, generate_stall_allocation_pdf_bytes

	data = extract_request_data(kwargs)
	reg_id = (data.get("registration_id") or data.get("id") or "").strip()
	mobile_no = (data.get("mobile_no") or "").strip()
	email = (data.get("email") or "").strip().lower()

	alloc_data = get_registration_allocation_data(registration_id=reg_id, mobile_no=mobile_no, email=email)
	if not alloc_data:
		is_browser = False
		if hasattr(frappe, "request") and getattr(frappe, "request", None):
			accept = (frappe.request.headers.get("Accept") or "").lower()
			if "text/html" in accept:
				is_browser = True

		if is_browser:
			clean_target = frappe.utils.escape_html(reg_id or mobile_no or email or "N/A")
			frappe.respond_as_web_page(
				title="Allocation Record Not Found",
				html=f"<p style='font-size:15px;'>No confirmed stall booking found for: <strong>{clean_target}</strong>.</p><p style='color:#64748b; font-size:13px;'>If this was a test booking that was reset or deleted, please check your Attendee Dashboard or contact support.</p>",
				http_status_code=404,
				indicator_color="red"
			)
			return

		frappe.local.response["http_status_code"] = 404
		return {"status": "error", "message": f"No booking registration found for {reg_id or mobile_no or email}"}

	pdf_bytes = generate_stall_allocation_pdf_bytes(alloc_data)
	booth_tag = str(alloc_data.get("stall_number") or alloc_data.get("registration_id") or "allotment").replace(" ", "_")
	filename = f"stall_allocation_{booth_tag}.pdf"

	frappe.local.response.filename = filename
	frappe.local.response.filecontent = pdf_bytes
	frappe.local.response.type = "download"
	frappe.local.response.content_type = "application/pdf"


