import os
import re
import urllib.parse
import concurrent.futures
import frappe
from frappe.utils import flt, now_datetime
from connectfest.services.common import extract_request_data, handle_api_errors

def get_email_header_style():
	return """
		font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
		color: #1e293b;
		line-height: 1.6;
		max-width: 620px;
		margin: 0 auto;
		background: #ffffff;
		border-radius: 12px;
		overflow: hidden;
		box-shadow: 0 4px 20px rgba(0,0,0,0.08);
		border: 1px solid #e2e8f0;
	"""


def get_base_url():
	"""
	Dynamically resolves the Frappe backend base URL for API endpoints and downloads:
	- LIVE Production: https://api.connectfesttn.in
	- UAT Environment: https://connectfest.mdqapps.online
	- Local Environment: http://127.0.0.1:8080
	"""
	# 1. Check HTTP Request context
	if hasattr(frappe, "request") and getattr(frappe, "request", None):
		host = (frappe.request.headers.get("Host") or getattr(frappe.request, "host", "") or "").strip().lower()
		if host:
			if "localhost" in host:
				port = host.split(":")[-1] if ":" in host else "8080"
				return f"http://127.0.0.1:{port}"
			elif "127.0.0.1" in host:
				return f"http://{host}".rstrip("/")
			elif "connectfesttn.in" in host:
				return "https://api.connectfesttn.in"
			elif "mdqapps.online" in host:
				return "https://connectfest.mdqapps.online"
			else:
				scheme = getattr(frappe.request, "scheme", "https")
				return f"{scheme}://{host}".rstrip("/")

	# 2. Check site config / site name
	site_name = (getattr(frappe.local, "site", "") or "").lower()
	if "connectfesttn.in" in site_name:
		return "https://api.connectfesttn.in"
	elif "mdqapps.online" in site_name:
		return "https://connectfest.mdqapps.online"
	elif "localhost" in site_name or "local" in site_name:
		return "http://127.0.0.1:8080"

	try:
		site_url = (frappe.utils.get_url() or "").strip().rstrip("/")
		if site_url:
			if "localhost" in site_url or "127.0.0.1" in site_url:
				return "http://127.0.0.1:8080"
			elif "connectfesttn.in" in site_url:
				return "https://api.connectfesttn.in"
			elif "mdqapps.online" in site_url:
				return "https://connectfest.mdqapps.online"
			elif site_url.startswith("http"):
				return site_url
	except Exception:
		pass

	return "https://api.connectfesttn.in"


def get_attendee_dashboard_url():
	"""
	Resolves the Attendee Dashboard URL dynamically:
	- Respects ConnectFest Settings (frontend_dashboard_url) if explicitly configured.
	- LIVE Production default: https://connectfesttn.in/
	- UAT Environment default: https://connectfestapp.mdqapps.online/
	- Local Environment default: http://localhost:3000/
	"""
	custom_url = None
	try:
		custom_url = frappe.db.get_single_value("ConnectFest Settings", "frontend_dashboard_url")
	except Exception:
		pass

	# Detect environment
	is_live = False
	is_uat = False
	is_local = False

	if hasattr(frappe, "request") and getattr(frappe, "request", None):
		host = (frappe.request.headers.get("Host") or getattr(frappe.request, "host", "") or "").strip().lower()
		if "connectfesttn.in" in host:
			is_live = True
		elif "mdqapps.online" in host:
			is_uat = True
		elif "localhost" in host or "127.0.0.1" in host:
			is_local = True

	if not (is_live or is_uat or is_local):
		site_name = (getattr(frappe.local, "site", "") or "").lower()
		if "connectfesttn.in" in site_name:
			is_live = True
		elif "mdqapps.online" in site_name:
			is_uat = True
		elif "localhost" in site_name or "local" in site_name:
			is_local = True

	default_frontend = "https://connectfesttn.in/"
	if is_uat:
		default_frontend = "https://connectfestapp.mdqapps.online/"
	elif is_local:
		default_frontend = "http://localhost:3000/"

	if custom_url and str(custom_url).strip():
		c_url = str(custom_url).strip()
		if c_url.startswith("http"):
			return c_url.rstrip("/") + "/"
		return f"{default_frontend.rstrip('/')}/{c_url.lstrip('/')}"

	return default_frontend


def build_confirmation_html(data):
	full_name = data.get("full_name") or "Valued Attendee"
	company_name = data.get("company_name") or ""
	company_str = f" ({company_name})" if company_name else ""
	reg_id = data.get("registration_id") or data.get("name") or "N/A"
	stall_numbers = data.get("stall_numbers") or data.get("stall_number") or "Assigned"
	stall_category = data.get("stall_category") or data.get("tier") or "Exhibitor"
	amount = flt(data.get("amount", 0.0))
	payment_mode = data.get("payment_mode") or "Online"
	payment_id = data.get("razorpay_payment_id") or data.get("receipt_id") or "N/A"
	payment_date = data.get("payment_date") or now_datetime().strftime("%d %b %Y, %I:%M %p")

	# Fetch dynamic event details from ConnectFest Settings
	event_name = "ConnectFest 2027"
	event_dates = "20 & 21 Feb 2027"
	event_venue = "Chennai Trade Centre, Nandambakkam"
	event_timings = "9:00 AM - 7:00 PM"
	try:
		settings = frappe.get_single("ConnectFest Settings")
		event_name = settings.get("event_name") or event_name
		event_dates = settings.get("event_dates") or event_dates
		event_venue = settings.get("event_venue") or event_venue
		event_timings = settings.get("event_timings") or event_timings
	except Exception:
		pass

	# Pass 1 Primary Exhibitor Pass Details (Only 1 QR sent in confirmation email)
	p1_code = data.get("pass_code") or f"{reg_id}-P1"
	# Encode clean pass_code directly via self-hosted local QR generator
	from connectfest.services.passes import get_pass_qr_url
	p1_qr_url = get_pass_qr_url(p1_code)

	base_url = get_base_url()
	download_qr_url = f"{base_url}/api/method/connectfest.api.download_pass_qr?code={urllib.parse.quote(str(p1_code))}"
	download_url = f"{base_url}/api/method/connectfest.api.download_stall_allocation_letter?registration_id={reg_id}"
	dashboard_url = get_attendee_dashboard_url()
	view_passes_url = f"{dashboard_url.rstrip('/')}/?view=passes&registration_id={reg_id}"

	return f"""
	<div style="{get_email_header_style()}">
		<div style="background-color: #4f46e5; background-image: linear-gradient(135deg, #4f46e5 0%, #7c3aed 100%); padding: 32px 24px; text-align: center; color: #ffffff; border-radius: 12px 12px 0 0;">
			<div style="font-size: 13px; font-weight: 700; letter-spacing: 2px; text-transform: uppercase; color: #c7d2fe; margin-bottom: 8px;">{event_name}</div>
			<h1 style="margin: 0; font-size: 26px; font-weight: 800; color: #ffffff;">Booking Confirmed! 🎉</h1>
			<p style="margin: 8px 0 0; font-size: 15px; color: #e0e7ff;">Your exhibitor stall is reserved & ready</p>
		</div>

		<div style="padding: 28px 24px;">
			<p style="font-size: 16px; margin: 0 0 16px; color: #334155;">
				Dear <strong>{full_name}</strong>{company_str},
			</p>
			<p style="font-size: 15px; margin: 0 0 24px; color: #475569;">
				Congratulations! Your payment has been received successfully and your stall booking for <strong>{event_name}</strong> is officially confirmed.
			</p>

			<div style="background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 8px; padding: 20px; margin-bottom: 24px;">
				<h3 style="margin: 0 0 16px; font-size: 15px; text-transform: uppercase; letter-spacing: 1px; color: #64748b; font-weight: 700;">Booking & Payment Receipt</h3>
				<table style="width: 100%; border-collapse: collapse; font-size: 14px;">
					<tr>
						<td style="padding: 8px 0; color: #64748b;">Stall Number(s):</td>
						<td style="padding: 8px 0; font-weight: 700; color: #4f46e5; text-align: right; font-size: 16px;">{stall_numbers}</td>
					</tr>
					<tr style="border-top: 1px solid #f1f5f9;">
						<td style="padding: 8px 0; color: #64748b;">Tier / Category:</td>
						<td style="padding: 8px 0; font-weight: 600; color: #0f172a; text-align: right;">{stall_category}</td>
					</tr>
					<tr style="border-top: 1px solid #f1f5f9;">
						<td style="padding: 8px 0; color: #64748b;">Amount Paid:</td>
						<td style="padding: 8px 0; font-weight: 700; color: #16a34a; text-align: right; font-size: 16px;">₹{amount:,.2f}</td>
					</tr>
					<tr style="border-top: 1px solid #f1f5f9;">
						<td style="padding: 8px 0; color: #64748b;">Payment Mode:</td>
						<td style="padding: 8px 0; font-weight: 600; color: #0f172a; text-align: right;">{payment_mode}</td>
					</tr>
					<tr style="border-top: 1px solid #f1f5f9;">
						<td style="padding: 8px 0; color: #64748b;">Reference / Receipt ID:</td>
						<td style="padding: 8px 0; font-family: monospace; font-size: 13px; color: #475569; text-align: right;">{payment_id}</td>
					</tr>
					<tr style="border-top: 1px solid #f1f5f9;">
						<td style="padding: 8px 0; color: #64748b;">Payment Date:</td>
						<td style="padding: 8px 0; color: #475569; text-align: right;">{payment_date}</td>
					</tr>
					<tr style="border-top: 1px solid #f1f5f9;">
						<td style="padding: 8px 0; color: #64748b;">Booking Status:</td>
						<td style="padding: 8px 0; font-weight: 700; color: #16a34a; text-align: right;">CONFIRMED</td>
					</tr>
				</table>
			</div>

			<div style="background: #ffffff; border: 1px solid #cbd5e1; border-radius: 10px; padding: 18px; margin-bottom: 24px; box-shadow: 0 1px 4px rgba(0,0,0,0.05);">
				<table style="width: 100%; border-collapse: collapse;">
					<tr>
						<td style="vertical-align: top; width: 62%;">
							<div style="display: inline-block; font-size: 11px; font-weight: 700; color: #4f46e5; background: #eef2ff; border: 1px solid #c7d2fe; padding: 2px 8px; border-radius: 4px; text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 6px;">Primary Exhibitor Digital Pass</div>
							<div style="font-size: 16px; font-weight: 800; color: #0f172a; margin-bottom: 3px;">{full_name}</div>
							<div style="font-size: 13px; color: #475569; margin-bottom: 6px;">{company_name or 'Exhibitor'}</div>
							<div style="font-size: 12px; color: #64748b;">Stall: <strong style="color: #0f172a;">{stall_numbers}</strong> ({stall_category})</div>
							<div style="font-size: 12px; font-family: monospace; color: #64748b; margin-top: 6px;">Pass Code: <strong style="color: #4f46e5;">{p1_code}</strong></div>
						</td>
						<td style="vertical-align: middle; text-align: center; width: 38%; padding-left: 12px; border-left: 1px solid #f1f5f9;">
							<a href="{download_qr_url}" target="_blank" download="ConnectFest_Pass_QR_{p1_code}.png" title="Click to download Gate Pass Card" style="text-decoration: none; display: inline-block;">
								<img embed="files/ConnectFest_Pass_QR_{p1_code}.png" src="{p1_qr_url}" alt="Gate Pass QR" width="115" height="115" style="display: block; margin: 0 auto; border: 1px solid #e2e8f0; border-radius: 6px; background: #ffffff; padding: 4px;" />
								<span style="font-size: 10px; color: #4f46e5; font-weight: 600; display: block; margin-top: 5px; text-decoration: underline;">📥 Click to Download Card</span>
							</a>
							<div style="font-size: 8.5px; font-weight: 800; color: #64748b; letter-spacing: 1px; margin-top: 5px; text-transform: uppercase;">SCAN AT ENTRY</div>
						</td>
					</tr>
				</table>
			</div>

			<div style="background: #eef2ff; border-left: 4px solid #4f46e5; padding: 16px; border-radius: 0 8px 8px 0; margin-bottom: 24px;">
				<h4 style="margin: 0 0 6px; color: #3730a3; font-size: 14px; font-weight: 700;">Event Venue & Schedule</h4>
				<p style="margin: 0; font-size: 13px; color: #4338ca; line-height: 1.6;">
					📅 <strong>Dates:</strong> {event_dates}<br>
					📍 <strong>Venue:</strong> {event_venue}<br>
					⏰ <strong>Timings:</strong> {event_timings}
				</p>
			</div>

			<div style="text-align: center; margin-top: 8px; margin-bottom: 28px;">
				<a href="{download_url}" style="background: #16a34a; color: #ffffff; text-decoration: none; padding: 14px 28px; border-radius: 6px; font-weight: 700; font-size: 14px; display: inline-block; box-shadow: 0 4px 12px rgba(22, 163, 74, 0.25);">
					📥 Download Allocation PDF
				</a>
			</div>
		</div>

		<div style="background: #f1f5f9; padding: 16px 24px; text-align: center; font-size: 12px; color: #94a3b8; border-top: 1px solid #e2e8f0;">
			<p style="margin: 0;">{event_name} • The Premier Business & Innovation Expo</p>
		</div>
	</div>
	"""


def build_visitor_confirmation_html(data):
	from connectfest.services.passes import get_pass_qr_url

	full_name = (data.get("full_name") or "Valued Visitor").strip()
	reg_id = data.get("registration_id") or data.get("name") or "N/A"
	pass_code = data.get("pass_code") or reg_id
	which_day = (data.get("which_day") or "Both days").strip()
	access_text = "BOTH DAYS" if which_day.lower() == "both days" else which_day.upper()

	# Fetch dynamic event details from ConnectFest Settings
	event_name = "ConnectFest 2027"
	event_dates = "20 & 21 Feb 2027"
	event_venue = "Chennai Trade Centre, Nandambakkam"
	event_timings = "9:00 AM - 7:00 PM"
	try:
		settings = frappe.get_single("ConnectFest Settings")
		event_name = settings.get("event_name") or event_name
		event_dates = settings.get("event_dates") or event_dates
		event_venue = settings.get("event_venue") or event_venue
		event_timings = settings.get("event_timings") or event_timings
	except Exception:
		pass

	base_url = get_base_url()
	qr_url = get_pass_qr_url(pass_code)
	download_qr_url = f"{base_url}/api/method/connectfest.api.download_pass_qr?code={urllib.parse.quote(str(pass_code))}"

	return f"""
	<div style="{get_email_header_style()}">
		<div style="background-color: #b91c1c; background-image: linear-gradient(135deg, #b91c1c 0%, #991b1b 100%); padding: 28px 24px; text-align: center; color: #ffffff; border-radius: 12px 12px 0 0;">
			<div style="font-size: 13px; font-weight: 700; letter-spacing: 2px; text-transform: uppercase; color: #fecaca; margin-bottom: 6px;">{event_name}</div>
			<h1 style="margin: 0; font-size: 24px; font-weight: 800; color: #ffffff;">Visitor Gate Pass 🎟️</h1>
			<p style="margin: 6px 0 0; font-size: 14px; color: #fee2e2;">Your Grand Expo entrance pass is ready</p>
		</div>

		<div style="padding: 24px;">
			<p style="font-size: 15px; margin: 0 0 16px; color: #334155;">
				Dear <strong>{full_name}</strong>,
			</p>
			<p style="font-size: 14px; margin: 0 0 20px; color: #475569;">
				Thank you for registering as a Visitor for <strong>{event_name}</strong>. Please present the QR code below at the entry gate for instant admission.
			</p>

			<!-- Simple Clean Visitor Pass Card (No Photo, Just QR & Essential Details) -->
			<div style="background: #ffffff; border: 1.5px solid #cbd5e1; border-radius: 12px; padding: 20px; margin-bottom: 20px; box-shadow: 0 2px 8px rgba(0,0,0,0.06);">
				<table style="width: 100%; border-collapse: collapse;">
					<tr>
						<td style="vertical-align: top; width: 58%; padding-right: 12px;">
							<div style="display: inline-block; font-size: 11px; font-weight: 700; color: #b91c1c; background: #fee2e2; border: 1px solid #fecaca; padding: 2px 8px; border-radius: 4px; text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 8px;">VISITOR DIGITAL PASS</div>
							<div style="font-size: 17px; font-weight: 800; color: #0f172a; margin-bottom: 6px; text-transform: uppercase;">{full_name}</div>
							<div style="font-size: 12.5px; font-family: monospace; color: #64748b; margin-bottom: 8px;">Pass Code: <strong style="color: #b91c1c; font-size: 13.5px;">{pass_code}</strong></div>
							<div style="font-size: 12px; color: #475569; margin-bottom: 5px;">Access: <strong style="color: #d97706;">{access_text}</strong></div>
							<div style="font-size: 12px; color: #475569; margin-bottom: 5px;">Venue: <strong>{event_venue}</strong></div>
							<div style="font-size: 12px; color: #475569;">Dates: <strong>{event_dates}</strong></div>
						</td>
						<td style="vertical-align: middle; text-align: center; width: 40%; border-left: 1px solid #f1f5f9; padding-left: 12px;">
							<a href="{download_qr_url}" target="_blank" download="ConnectFest_Pass_QR_{pass_code}.png" title="Click to download Visitor Pass Card" style="text-decoration: none; display: inline-block;">
								<img embed="files/ConnectFest_Pass_QR_{pass_code}.png" src="{qr_url}" alt="Visitor Pass QR" width="120" height="120" style="display: block; margin: 0 auto; border: 1px solid #e2e8f0; border-radius: 8px; background: #ffffff; padding: 4px;" />
								<span style="font-size: 10px; color: #b91c1c; font-weight: 600; display: block; margin-top: 5px; text-decoration: underline;">📥 Click to Download Card</span>
							</a>
							<div style="font-size: 8.5px; font-weight: 800; color: #64748b; letter-spacing: 1px; margin-top: 5px; text-transform: uppercase;">SCAN AT ENTRY</div>
						</td>
					</tr>
				</table>
			</div>

			<div style="background: #fef2f2; border-left: 4px solid #b91c1c; padding: 14px 16px; border-radius: 0 8px 8px 0;">
				<h4 style="margin: 0 0 4px; color: #991b1b; font-size: 13px; font-weight: 700;">Event Venue & Schedule</h4>
				<p style="margin: 0; font-size: 12.5px; color: #7f1d1d; line-height: 1.5;">
					📅 <strong>Dates:</strong> {event_dates}<br>
					📍 <strong>Venue:</strong> {event_venue}<br>
					⏰ <strong>Timings:</strong> {event_timings}
				</p>
			</div>
		</div>

		<div style="background: #f1f5f9; padding: 14px 20px; text-align: center; font-size: 12px; color: #94a3b8; border-top: 1px solid #e2e8f0;">
			<p style="margin: 0;">{event_name} • The Premier Business & Innovation Expo</p>
		</div>
	</div>
	"""


def build_failure_html(data):
	full_name = data.get("full_name") or "Valued Attendee"
	reg_id = data.get("registration_id") or "N/A"
	stall_number = data.get("stall_number") or "N/A"
	error_message = data.get("error_message") or "Transaction could not be completed or signature verification failed."
	now_str = now_datetime().strftime("%d %b %Y, %I:%M %p")

	return f"""
	<div style="{get_email_header_style()}">
		<div style="background-color: #dc2626; background-image: linear-gradient(135deg, #dc2626 0%, #b91c1c 100%); padding: 32px 24px; text-align: center; color: #ffffff; border-radius: 12px 12px 0 0;">
			<div style="font-size: 13px; font-weight: 700; letter-spacing: 2px; text-transform: uppercase; color: #fecaca; margin-bottom: 8px;">ConnectFest 2027</div>
			<h1 style="margin: 0; font-size: 24px; font-weight: 800; color: #ffffff;">Payment Failed ⚠️</h1>
			<p style="margin: 8px 0 0; font-size: 15px; color: #fee2e2;">Your stall booking could not be confirmed</p>
		</div>

		<div style="padding: 28px 24px;">
			<p style="font-size: 16px; margin: 0 0 16px; color: #334155;">
				Dear <strong>{full_name}</strong>,
			</p>
			<p style="font-size: 15px; margin: 0 0 20px; color: #475569;">
				We encountered an issue while processing your payment for stall booking at <strong>ConnectFest 2027</strong>. Your registration is currently in <strong>Pending</strong> status.
			</p>

			<div style="background: #fef2f2; border: 1px solid #fee2e2; border-left: 4px solid #ef4444; border-radius: 4px 8px 8px 4px; padding: 18px; margin-bottom: 24px;">
				<h4 style="margin: 0 0 8px; color: #991b1b; font-size: 14px; font-weight: 700;">Failure Details</h4>
				<table style="width: 100%; border-collapse: collapse; font-size: 14px;">
					<tr>
						<td style="padding: 4px 0; color: #7f1d1d; width: 40%;">Registration ID:</td>
						<td style="padding: 4px 0; font-weight: 600; color: #991b1b;">{reg_id}</td>
					</tr>
					<tr>
						<td style="padding: 4px 0; color: #7f1d1d;">Requested Stall:</td>
						<td style="padding: 4px 0; font-weight: 600; color: #991b1b;">{stall_number}</td>
					</tr>
					<tr>
						<td style="padding: 4px 0; color: #7f1d1d;">Attempt Date:</td>
						<td style="padding: 4px 0; color: #991b1b;">{now_str}</td>
					</tr>
					<tr>
						<td style="padding: 4px 0; color: #7f1d1d; vertical-align: top;">Reason:</td>
						<td style="padding: 4px 0; color: #dc2626; font-weight: 500;">{error_message}</td>
					</tr>
				</table>
			</div>

			<div style="background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 8px; padding: 16px; margin-bottom: 24px;">
				<h4 style="margin: 0 0 6px; font-size: 14px; color: #0f172a; font-weight: 600;">Was your bank account or card debited?</h4>
				<p style="margin: 0; font-size: 13px; color: #64748b; line-height: 1.5;">
					Don't worry! If money was deducted, our payment gateway will automatically reverse it to your original payment source within <strong>5 to 7 business days</strong>.
				</p>
			</div>

			<p style="font-size: 14px; color: #475569; line-height: 1.5; margin: 0 0 24px;">
				You can retry completing your stall payment or choose an alternate payment method (Online / UPI / Cash at desk) by logging into your dashboard.
			</p>

			<div style="text-align: center; margin-bottom: 24px;">
				<a href="{get_attendee_dashboard_url()}" style="background: #dc2626; color: #ffffff; text-decoration: none; padding: 12px 28px; border-radius: 6px; font-weight: 600; font-size: 14px; display: inline-block;">Retry Payment Now</a>
			</div>
		</div>

		<div style="background: #f1f5f9; padding: 16px 24px; text-align: center; font-size: 12px; color: #94a3b8; border-top: 1px solid #e2e8f0;">
			<p style="margin: 0;">ConnectFest 2027 • The Premier Business & Innovation Expo</p>
		</div>
	</div>
	"""


def send_booking_confirmation_email(registration_id=None, email=None, **kwargs):
	"""
	Sends modern HTML booking confirmation email to attendee.
	Gracefully handles SMTP availability.
	"""
	try:
		reg_data = {}
		if registration_id:
			records = frappe.db.sql("""
				SELECT name, registration_id, full_name, company_name, email, mobile_no,
				       stall_number, stall_category, amount, payment_status, payment_mode,
				       razorpay_payment_id, payment_date,
				       pass_code, qr_payload, pass_1_image,
				       pass_2_name, pass_2_image, pass_2_mobile, pass_2_email,
				       pass_2_pass_code, pass_2_qr_payload
				FROM `tabConnectFest Registration`
				WHERE registration_id = %(id)s OR name = %(id)s
				LIMIT 1
			""", {"id": registration_id}, as_dict=True)
			if records:
				reg_data = records[0]

		# Override with kwargs if supplied
		for k, v in kwargs.items():
			if v: reg_data[k] = v

		target_email = email or reg_data.get("email")
		if not target_email or "@" not in target_email:
			return {"status": "skipped", "message": "No valid email address found for attendee"}

		stall_display = reg_data.get("stall_numbers") or reg_data.get("stall_number") or "Assigned"
		subject = f"🎉 Booking Confirmed: Stall {stall_display} at ConnectFest 2027!"
		html_body = build_confirmation_html(reg_data)

		has_outgoing = bool(frappe.db.get_value("Email Account", {"default_outgoing": 1, "enable_outgoing": 1}))
		if not has_outgoing:
			if hasattr(frappe.local, "message_log"):
				frappe.local.message_log = [
					m for m in getattr(frappe.local, "message_log", [])
					if "Email Account" not in str(m)
				]
			return {
				"status": "skipped",
				"message": "Email skipped: Default outgoing Email Account not configured in ERPNext/Frappe",
				"recipient": target_email,
				"registration_id": reg_data.get("registration_id")
			}

		# Fast Concurrent PDF generation alongside HTML build
		attachments = []
		try:
			from connectfest.services.invoice import get_registration_allocation_data, generate_stall_allocation_pdf_bytes
			alloc_data = get_registration_allocation_data(registration_id=reg_data.get("registration_id") or reg_data.get("name"))
			if not alloc_data:
				alloc_data = reg_data

			with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
				pdf_future = executor.submit(generate_stall_allocation_pdf_bytes, alloc_data)
				try:
					pdf_bytes = pdf_future.result(timeout=6)
					stall_tag = str(reg_data.get("stall_numbers") or reg_data.get("stall_number") or reg_data.get("registration_id") or "allotment").replace(" ", "_").replace("-", "")
					attachments.append({
						"fname": f"Stall_Allocation_{stall_tag}.pdf",
						"fcontent": pdf_bytes
					})
				except Exception:
					frappe.log_error(title="Failed to generate allocation PDF attachment", message=frappe.get_traceback())
		except Exception:
			frappe.log_error(title="Failed to process allocation PDF attachment", message=frappe.get_traceback())

		# Prepare Inline CID QR Image for Outlook / Gmail automatic display
		inline_images = []
		try:
			from connectfest.services.passes import generate_local_qr_png_bytes
			p1_code = reg_data.get("pass_code") or f"{reg_data.get('registration_id') or reg_data.get('name')}-P1"
			p1_qr_bytes = generate_local_qr_png_bytes(p1_code, scale=8, border=2)
			qr_filename = f"files/ConnectFest_Pass_QR_{p1_code}.png"
			inline_images.append({"filename": qr_filename, "filecontent": p1_qr_bytes})
			import os
			public_dir = frappe.get_site_path("public", "files")
			os.makedirs(public_dir, exist_ok=True)
			with open(os.path.join(public_dir, f"ConnectFest_Pass_QR_{p1_code}.png"), "wb") as f:
				f.write(p1_qr_bytes)
		except Exception:
			pass

		# Fast, High-Priority Email Dispatch (never blocked behind external API calls)
		frappe.sendmail(
			recipients=[target_email],
			subject=subject,
			message=html_body,
			attachments=attachments if attachments else None,
			inline_images=inline_images if inline_images else None,
			add_css=False,
			now=True,
			delayed=False,
			x_priority=1,
			send_priority=1,
		)
		frappe.db.commit()

		# Trigger WhatsApp Confirmation via WATI for exhibitor booking
		try:
			from connectfest.services.wati import send_booking_confirmation_whatsapp
			send_booking_confirmation_whatsapp(
				registration_id=reg_data.get("registration_id") or reg_data.get("name"),
				mobile_no=reg_data.get("mobile_no"),
				reg_data=reg_data,
			)
		except Exception as wati_err:
			frappe.log_error(title="ConnectFest WhatsApp Send Error in Notification", message=str(wati_err))

		if hasattr(frappe.local, "message_log"):
			frappe.local.message_log = [
				m for m in frappe.local.message_log
				if "Email Account" not in str(m)
			]

		return {
			"status": "success",
			"message": f"Confirmation email queued/sent to {target_email}",
			"recipient": target_email,
			"registration_id": reg_data.get("registration_id")
		}
	except Exception as e:
		frappe.log_error(title="ConnectFest Send Confirmation Email Error", message=frappe.get_traceback())
		if hasattr(frappe.local, "message_log"):
			frappe.local.message_log = [
				m for m in frappe.local.message_log
				if "Email Account" not in str(m)
			]
		return {"status": "error", "message": str(e)}


def send_payment_failure_email(registration_id=None, email=None, error_message=None, **kwargs):
	"""
	Sends modern HTML payment failure notification to attendee.
	Gracefully handles SMTP availability.
	"""
	try:
		reg_data = {}
		if registration_id:
			records = frappe.db.sql("""
				SELECT name, registration_id, full_name, email, mobile_no, stall_number, stall_category
				FROM `tabConnectFest Registration`
				WHERE registration_id = %(id)s OR name = %(id)s
				LIMIT 1
			""", {"id": registration_id}, as_dict=True)
			if records:
				reg_data = records[0]

		for k, v in kwargs.items():
			if v: reg_data[k] = v

		if error_message:
			reg_data["error_message"] = error_message

		target_email = email or reg_data.get("email")
		if not target_email or "@" not in target_email:
			return {"status": "skipped", "message": "No valid email address found for attendee"}

		stall_display = reg_data.get("stall_number") or "Stall"
		subject = f"⚠️ Payment Failed - ConnectFest 2027 Stall Booking ({stall_display})"
		html_body = build_failure_html(reg_data)

		has_outgoing = bool(frappe.db.get_value("Email Account", {"default_outgoing": 1, "enable_outgoing": 1}))
		if not has_outgoing:
			if hasattr(frappe.local, "message_log"):
				frappe.local.message_log = [
					m for m in getattr(frappe.local, "message_log", [])
					if "Email Account" not in str(m)
				]
			return {
				"status": "skipped",
				"message": "Email skipped: Default outgoing Email Account not configured in ERPNext/Frappe",
				"recipient": target_email,
				"registration_id": reg_data.get("registration_id")
			}

		frappe.sendmail(
			recipients=[target_email],
			subject=subject,
			message=html_body,
			now=True,
			delayed=False,
		)
		frappe.db.commit()

		if hasattr(frappe.local, "message_log"):
			frappe.local.message_log = [
				m for m in frappe.local.message_log
				if "Email Account" not in str(m)
			]

		return {
			"status": "success",
			"message": f"Failure alert email queued/sent to {target_email}",
			"recipient": target_email,
			"registration_id": reg_data.get("registration_id")
		}
	except Exception as e:
		frappe.log_error(title="ConnectFest Send Failure Email Error", message=frappe.get_traceback())
		if hasattr(frappe.local, "message_log"):
			frappe.local.message_log = [
				m for m in frappe.local.message_log
				if "Email Account" not in str(m)
			]
		return {"status": "error", "message": str(e)}


@handle_api_errors("ConnectFest Resend Booking Email Error")
def resend_booking_email(registration_id=None, email=None, **kwargs):
	"""
	API to explicitly resend booking confirmation email.
	"""
	data = extract_request_data(kwargs)
	reg_id = (registration_id or data.get("registration_id") or "").strip()
	em = (email or data.get("email") or "").strip().lower()

	if not reg_id and not em:
		frappe.local.response["http_status_code"] = 400
		return {"status": "error", "message": "registration_id or email is required"}

	return send_booking_confirmation_email(registration_id=reg_id, email=em)


@frappe.whitelist(allow_guest=True)
@handle_api_errors("ConnectFest Resend Booking WhatsApp Error")
def resend_booking_whatsapp(registration_id=None, mobile_no=None, **kwargs):
	"""
	API to explicitly resend booking confirmation WhatsApp message via WATI.
	"""
	from connectfest.services.wati import resend_booking_whatsapp as _resend_whatsapp
	return _resend_whatsapp(registration_id=registration_id, mobile_no=mobile_no, **kwargs)


def send_visitor_confirmation_email(registration_id=None, email=None, pass_code=None, **kwargs):
	"""
	Sends official Visitor Gate Pass confirmation email to attendee with high-res printable PDF badge attachment.
	Gracefully handles SMTP availability.
	"""
	try:
		from connectfest.services.visitor_badge import get_visitor_badge_data

		reg_data = {}
		clean_code = (pass_code or "").strip()
		clean_reg = (registration_id or "").strip()
		clean_em = (email or "").strip().lower()

		rec = get_visitor_badge_data(pass_code=clean_code, registration_id=clean_reg, email=clean_em)
		if rec:
			reg_data = dict(rec)

		for k, v in kwargs.items():
			if v: reg_data[k] = v

		target_email = clean_em or reg_data.get("email")
		if not target_email or "@" not in target_email:
			return {"status": "skipped", "message": "No valid email address found for attendee"}

		pcode = reg_data.get("pass_code") or reg_data.get("registration_id") or "VISITOR"
		subject = f"🎟️ Your Visitor Gate Pass: ConnectFest 2027 ({pcode})"
		html_body = build_visitor_confirmation_html(reg_data)

		has_outgoing = bool(frappe.db.get_value("Email Account", {"default_outgoing": 1, "enable_outgoing": 1}))
		if not has_outgoing:
			if hasattr(frappe.local, "message_log"):
				frappe.local.message_log = [
					m for m in getattr(frappe.local, "message_log", [])
					if "Email Account" not in str(m)
				]
			return {
				"status": "skipped",
				"message": "Email skipped: Default outgoing Email Account not configured in ERPNext/Frappe",
				"recipient": target_email,
				"registration_id": reg_data.get("registration_id")
			}

		# Prepare Inline CID QR Image for Outlook / Gmail automatic display
		inline_images = []
		try:
			from connectfest.services.passes import generate_local_qr_png_bytes
			vis_qr_bytes = generate_local_qr_png_bytes(pcode, scale=8, border=2)
			qr_filename = f"files/ConnectFest_Pass_QR_{pcode}.png"
			inline_images.append({"filename": qr_filename, "filecontent": vis_qr_bytes})
			import os
			public_dir = frappe.get_site_path("public", "files")
			os.makedirs(public_dir, exist_ok=True)
			with open(os.path.join(public_dir, f"ConnectFest_Pass_QR_{pcode}.png"), "wb") as f:
				f.write(vis_qr_bytes)
		except Exception:
			pass

		frappe.sendmail(
			recipients=[target_email],
			subject=subject,
			message=html_body,
			inline_images=inline_images if inline_images else None,
			add_css=False,
			now=True,
			delayed=False,
			x_priority=1,
			send_priority=1,
		)
		frappe.db.commit()

		if hasattr(frappe.local, "message_log"):
			frappe.local.message_log = [
				m for m in frappe.local.message_log
				if "Email Account" not in str(m)
			]

		return {
			"status": "success",
			"message": f"Visitor confirmation email queued/sent to {target_email}",
			"recipient": target_email,
			"registration_id": reg_data.get("registration_id"),
			"pass_code": pcode,
		}
	except Exception as e:
		frappe.log_error(title="ConnectFest Send Visitor Confirmation Email Error", message=frappe.get_traceback())
		if hasattr(frappe.local, "message_log"):
			frappe.local.message_log = [
				m for m in frappe.local.message_log
				if "Email Account" not in str(m)
			]
		return {"status": "error", "message": str(e)}


@handle_api_errors("ConnectFest Resend Visitor Email Error")
def resend_visitor_email(registration_id=None, email=None, pass_code=None, **kwargs):
	"""
	API to explicitly resend visitor confirmation email.
	"""
	data = extract_request_data(kwargs)
	reg_id = (registration_id or data.get("registration_id") or "").strip()
	em = (email or data.get("email") or "").strip().lower()
	pcode = (pass_code or data.get("pass_code") or "").strip()

	if not reg_id and not em and not pcode:
		frappe.local.response["http_status_code"] = 400
		return {"status": "error", "message": "registration_id, pass_code, or email is required"}

	return send_visitor_confirmation_email(registration_id=reg_id, email=em, pass_code=pcode)

