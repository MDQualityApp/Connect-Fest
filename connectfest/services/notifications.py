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


def build_confirmation_html(data):
	full_name = data.get("full_name") or "Valued Attendee"
	company_name = data.get("company_name") or ""
	company_str = f" ({company_name})" if company_name else ""
	reg_id = data.get("registration_id") or "N/A"
	stall_numbers = data.get("stall_numbers") or data.get("stall_number") or "Assigned"
	stall_category = data.get("stall_category") or data.get("tier") or "Exhibitor"
	amount = flt(data.get("amount", 0.0))
	payment_mode = data.get("payment_mode") or "Online"
	payment_id = data.get("razorpay_payment_id") or data.get("receipt_id") or "N/A"
	payment_date = data.get("payment_date") or now_datetime().strftime("%d %b %Y, %I:%M %p")

	return f"""
	<div style="{get_email_header_style()}">
		<div style="background: linear-gradient(135deg, #4f46e5 0%, #7c3aed 100%); padding: 32px 24px; text-align: center; color: #ffffff;">
			<div style="font-size: 13px; font-weight: 700; letter-spacing: 2px; text-transform: uppercase; color: #c7d2fe; margin-bottom: 8px;">ConnectFest 2027</div>
			<h1 style="margin: 0; font-size: 26px; font-weight: 800; color: #ffffff;">Booking Confirmed! 🎉</h1>
			<p style="margin: 8px 0 0; font-size: 15px; color: #e0e7ff;">Your exhibitor stall is reserved & ready</p>
		</div>

		<div style="padding: 28px 24px;">
			<p style="font-size: 16px; margin: 0 0 16px; color: #334155;">
				Dear <strong>{full_name}</strong>{company_str},
			</p>
			<p style="font-size: 15px; margin: 0 0 24px; color: #475569;">
				Thank you! Your payment has been received successfully and your stall booking for <strong>ConnectFest 2027</strong> is officially confirmed.
			</p>

			<div style="background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 8px; padding: 20px; margin-bottom: 24px;">
				<h3 style="margin: 0 0 16px; font-size: 15px; text-transform: uppercase; letter-spacing: 1px; color: #64748b; font-weight: 700;">Booking & Payment Receipt</h3>
				<table style="width: 100%; border-collapse: collapse; font-size: 14px;">
					<tr>
						<td style="padding: 8px 0; color: #64748b;">Registration ID:</td>
						<td style="padding: 8px 0; font-weight: 600; color: #0f172a; text-align: right;">{reg_id}</td>
					</tr>
					<tr style="border-top: 1px solid #f1f5f9;">
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

			<div style="background: #eef2ff; border-left: 4px solid #4f46e5; padding: 16px; border-radius: 0 8px 8px 0; margin-bottom: 24px;">
				<h4 style="margin: 0 0 6px; color: #3730a3; font-size: 14px; font-weight: 700;">Event Venue & Schedule</h4>
				<p style="margin: 0; font-size: 13px; color: #4338ca;">
					📅 <strong>Dates:</strong> 20 & 21 February 2027<br>
					📍 <strong>Venue:</strong> Chennai Trade Centre, Nandambakkam, Chennai, Tamil Nadu<br>
					⏰ <strong>Exhibitor Entry:</strong> 08:30 AM onwards
				</p>
			</div>

			<p style="font-size: 14px; color: #64748b; line-height: 1.5; margin: 0 0 24px;">
				You can access your digital exhibitor badge, floor plan coordinates, and event passes at any time through your Attendee Dashboard using your registered email or phone number.
			</p>

			<div style="text-align: center; margin-bottom: 24px;">
				<a href="https://connectfestapp.mdqapps.online/" style="background: #4f46e5; color: #ffffff; text-decoration: none; padding: 12px 28px; border-radius: 6px; font-weight: 600; font-size: 14px; display: inline-block;">View Attendee Dashboard</a>
			</div>
		</div>

		<div style="background: #f1f5f9; padding: 16px 24px; text-align: center; font-size: 12px; color: #94a3b8; border-top: 1px solid #e2e8f0;">
			<p style="margin: 0 0 4px;">ConnectFest 2027 • The Premier Business & Innovation Expo</p>
			<p style="margin: 0;">Need help? Email support@connectfest2027.com or contact event desk.</p>
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
		<div style="background: linear-gradient(135deg, #dc2626 0%, #b91c1c 100%); padding: 32px 24px; text-align: center; color: #ffffff;">
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
				<a href="https://connectfestapp.mdqapps.online/" style="background: #dc2626; color: #ffffff; text-decoration: none; padding: 12px 28px; border-radius: 6px; font-weight: 600; font-size: 14px; display: inline-block;">Retry Payment Now</a>
			</div>
		</div>

		<div style="background: #f1f5f9; padding: 16px 24px; text-align: center; font-size: 12px; color: #94a3b8; border-top: 1px solid #e2e8f0;">
			<p style="margin: 0 0 4px;">ConnectFest 2027 • The Premier Business & Innovation Expo</p>
			<p style="margin: 0;">Assistance needed? Email support@connectfest2027.com or call +91 98400 00000</p>
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
				       razorpay_payment_id, payment_date
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

		frappe.sendmail(
			recipients=[target_email],
			subject=subject,
			message=html_body,
			now=True,
			delayed=False,
		)
		frappe.db.commit()

		try:
			from frappe.email.queue import flush
			flush(from_test=False)
		except Exception:
			pass

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

		try:
			from frappe.email.queue import flush
			flush(from_test=False)
		except Exception:
			pass

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
