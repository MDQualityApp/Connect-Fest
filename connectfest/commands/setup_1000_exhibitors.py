import frappe
from frappe.utils import now_datetime, flt
import uuid

def generate_1000_stalls_and_users():
    """
    Populates 1,000+ stalls, 1,000 Users, and 1,000 Confirmed & Paid Registrations in ConnectFest.
    Preserves existing user rithinaveena2001@gmail.com and stall A-01.
    """
    frappe.init(site="connectfest.localhost")
    frappe.connect()

    print("=== STARTING 1,000+ USERS & STALLS GENERATION ===")

    now_str = now_datetime().strftime("%Y-%m-%d %H:%M:%S")

    # 1. Expand Stalls to 1,070+ if needed
    current_stall_count = frappe.db.count("ConnectFest Stall")
    print(f"Current Stall Count: {current_stall_count}")

    pricing = {
        "Regular": 25000.0,
        "Standard": 35000.0,
        "Premium": 45000.0,
        "Silver": 100000.0
    }

    blocks_to_add = [
        "Q", "R", "S", "T", "U", "V", "W", "X", "Y", "Z",
        "AA", "AB", "AC", "AD", "AE", "AF", "AG", "AH"
    ]
    tiers_cycle = ["Standard", "Premium", "Regular", "Silver"]
    zones_map = {
        "Standard": "Zone B - West Aisles",
        "Premium": "Zone C - Center Grand Aisles",
        "Regular": "Zone E - South Entrance Corridor",
        "Silver": "Zone D - East Pavilions"
    }
    dims_map = {
        "Standard": "3 x 2 m",
        "Premium": "3 x 3 m",
        "Regular": "2 x 2 m",
        "Silver": "4 x 3 m"
    }

    new_stalls = []
    stall_idx = 0
    for block in blocks_to_add:
        for num in range(1, 51):
            s_num = f"{block}-{num:02d}"
            if not frappe.db.exists("ConnectFest Stall", s_num):
                tier = tiers_cycle[stall_idx % len(tiers_cycle)]
                price = pricing[tier]
                zone = zones_map[tier]
                dims = dims_map[tier]
                new_stalls.append((
                    s_num, s_num, tier, dims, zone, price, "Available",
                    0, block, s_num, 100 + (stall_idx % 20) * 40, 100 + (stall_idx // 20) * 40,
                    30, 30, now_str, now_str, "Administrator", "Administrator"
                ))
            stall_idx += 1

    if new_stalls:
        frappe.db.sql("""
            INSERT INTO `tabConnectFest Stall`
            (name, stall_number, tier, dimensions, zone, price, status, is_blocked_by_admin, block, label, x, y, width, height, creation, modified, modified_by, owner)
            VALUES """ + ", ".join(["(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"] * len(new_stalls)),
            [val for sub in new_stalls for val in sub]
        )
        frappe.db.commit()
        print(f"Created {len(new_stalls)} new stalls.")

    total_stalls = frappe.db.count("ConnectFest Stall")
    print(f"Total Stalls in System: {total_stalls}")

    # 2. Get Available Stalls (excluding A-01)
    available_stalls = frappe.db.sql("""
        SELECT name, stall_number, tier, price
        FROM `tabConnectFest Stall`
        WHERE stall_number != 'A-01' AND (status = 'Available' OR status IS NULL OR status = '')
        ORDER BY stall_number ASC
        LIMIT 1000
    """, as_dict=True)

    print(f"Found {len(available_stalls)} available stalls to allocate to 1,000 users.")
    if len(available_stalls) < 1000:
        # If any are already booked by previous test runs, fetch all non-A-01 stalls
        available_stalls = frappe.db.sql("""
            SELECT name, stall_number, tier, price
            FROM `tabConnectFest Stall`
            WHERE stall_number != 'A-01'
            ORDER BY stall_number ASC
            LIMIT 1000
        """, as_dict=True)

    # 3. Bulk Prepare 1,000 Users, Registrations, and Stall Bookings
    user_rows = []
    role_rows = []
    reg_rows = []
    stall_updates = []

    first_names = ["Arun", "Bala", "Chandran", "Deepak", "Elango", "Ganesh", "Hari", "Ilan", "Jaya", "Karthik",
                   "Magesh", "Naveen", "Pradeep", "Rajesh", "Suresh", "Thiru", "Vijay", "Yash", "Zahir", "Praveen"]
    companies = ["Infotech", "Solutions", "Robotics", "Systems", "Technologies", "Enterprises", "Networks", "Labs", "Digital", "Industries"]

    for i in range(1, 1001):
        email = f"exhibitor{i:04d}@connectfest.in"
        fn = first_names[(i - 1) % len(first_names)]
        ln = f"Kumar {i:04d}"
        full_name = f"{fn} {ln}"
        comp = f"{fn} {companies[(i - 1) % len(companies)]} Pvt Ltd"
        mob = f"98{i:08d}"
        reg_id = f"CF-2610-{10000 + i:06d}"
        doc_name = f"CF-{20000 + i:05d}"
        
        stall_info = available_stalls[i - 1]
        stall_num = stall_info["stall_number"]
        tier = stall_info["tier"]
        price = flt(pricing.get(tier, stall_info["price"]))

        # User row
        user_rows.append((
            email, email, fn, ln, full_name, mob, mob, 1, "Website User",
            now_str, now_str, "Administrator", "Administrator", 0, 0
        ))

        # Role row
        role_rows.append((
            f"role_{uuid.uuid4().hex[:10]}", now_str, now_str, "Administrator", "Administrator",
            0, 0, "Website User", email, "roles", "User"
        ))

        # Registration row
        reg_rows.append((
            doc_name, now_str, now_str, "Administrator", "Administrator", 0, 0,
            reg_id, full_name, comp, "Chennai", mob, email, "Confirmed",
            0, "", "", "Technology", "Software & Cloud", "Social Media",
            tier, price, "Paid", f"order_cf1k_{i:06d}", f"pay_cf1k_{i:06d}",
            "verified_scale_test", now_str, stall_num, "Online"
        ))

        # Stall update: stall_num, reg_id, full_name, comp, email, mob
        stall_updates.append((reg_id, full_name, comp, email, mob, stall_num))

    # 4. Clean up any previous test batch
    print("Clearing previous batch test data if any...")
    frappe.db.sql("DELETE FROM `tabUser` WHERE email LIKE 'exhibitor%@connectfest.in'")
    frappe.db.sql("DELETE FROM `tabHas Role` WHERE parent LIKE 'exhibitor%@connectfest.in'")
    frappe.db.sql("DELETE FROM `tabConnectFest Registration` WHERE email LIKE 'exhibitor%@connectfest.in'")
    frappe.db.commit()

    # 5. Bulk Insert Users
    print("Inserting 1,000 Users...")
    frappe.db.sql("""
        INSERT INTO `tabUser`
        (name, email, first_name, last_name, full_name, mobile_no, phone, enabled, user_type, creation, modified, owner, modified_by, docstatus, idx)
        VALUES """ + ", ".join(["(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"] * len(user_rows)),
        [val for sub in user_rows for val in sub]
    )

    # 6. Bulk Insert Roles
    print("Inserting 1,000 User Roles...")
    frappe.db.sql("""
        INSERT INTO `tabHas Role`
        (name, creation, modified, owner, modified_by, docstatus, idx, role, parent, parentfield, parenttype)
        VALUES """ + ", ".join(["(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"] * len(role_rows)),
        [val for sub in role_rows for val in sub]
    )

    # 7. Bulk Insert Registrations
    print("Inserting 1,000 ConnectFest Registrations...")
    frappe.db.sql("""
        INSERT INTO `tabConnectFest Registration`
        (name, creation, modified, owner, modified_by, docstatus, idx, registration_id, full_name, company_name, location, mobile_no, email, status, is_bni_member, region_name, chapter_name, industry, segment, source_of_info, stall_category, amount, payment_status, razorpay_order_id, razorpay_payment_id, razorpay_signature, payment_date, stall_number, payment_mode)
        VALUES """ + ", ".join(["(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"] * len(reg_rows)),
        [val for sub in reg_rows for val in sub]
    )

    # 8. Bulk Update Stalls
    print("Updating 1,000 Stalls to Booked status...")
    for reg_id, full_name, comp, email, mob, stall_num in stall_updates:
        frappe.db.sql("""
            UPDATE `tabConnectFest Stall`
            SET status = 'Booked',
                booked_by_registration = %s,
                booked_by_name = %s,
                booked_by_company = %s,
                booked_by_email = %s,
                booked_by_mobile = %s,
                payment_mode = 'Online',
                hold_token = NULL,
                hold_expires_at = NULL
            WHERE stall_number = %s
        """, (reg_id, full_name, comp, email, mob, stall_num))

    frappe.db.commit()

    # Clear caches
    frappe.cache().delete_keys("connectfest:*")
    frappe.cache().delete_keys("cf:*")

    total_users = frappe.db.count("User")
    total_regs = frappe.db.count("ConnectFest Registration")
    total_booked_stalls = frappe.db.count("ConnectFest Stall", filters={"status": "Booked"})

    print("=== SUCCESS! ===")
    print(f"Total Users in User DocType: {total_users}")
    print(f"Total Registrations in ConnectFest Registration DocType: {total_regs}")
    print(f"Total Stalls in ConnectFest Stall DocType: {total_stalls}")
    print(f"Total Booked Stalls: {total_booked_stalls}")

if __name__ == "__main__":
    generate_1000_stalls_and_users()
