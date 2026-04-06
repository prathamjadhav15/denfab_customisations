import frappe
from frappe.utils import getdate, get_first_day, today

# --- Configuration ---
LATE_ENTRY_FREE_QUOTA = 3          # First N late entries are acceptable
LATE_ENTRY_DEDUCTION_INTERVAL = 3  # Add 1 LWP per N late entries after quota
# ----------------------


def deduct_leave_for_late_entries():
	"""
	Daily scheduler (4 AM): Add Leave Without Pay for excessive late entries.

	Rule:
	  - First 3 late entries in the month are free.
	  - For every 3rd late entry beyond that, add 1 Leave Without Pay.

	Examples:
	  3 late entries  → 0 LWP
	  4, 5 late       → 0 LWP
	  6 late          → 1 LWP
	  9 late          → 2 LWP
	  12 late         → 3 LWP
	"""
	today_date = getdate(today())
	from_date = get_first_day(today_date)
	to_date = today_date
	month_key = from_date.strftime("%Y-%m")

	frappe.logger().info(f"[Late Entry Deduction] Processing for {month_key}")

	late_entries = frappe.db.sql(
		"""
		SELECT employee, employee_name, COUNT(*) AS late_count
		FROM `tabAttendance`
		WHERE late_entry = 1
		  AND status IN ('Present', 'Half Day')
		  AND attendance_date BETWEEN %s AND %s
		  AND docstatus = 1
		GROUP BY employee
		""",
		(from_date, to_date),
		as_dict=True,
	)

	if not late_entries:
		frappe.logger().info(f"[Late Entry Deduction] No late entries found for {month_key}")
		return

	for row in late_entries:
		if row.late_count <= LATE_ENTRY_FREE_QUOTA:
			continue

		lwp_needed = (row.late_count - LATE_ENTRY_FREE_QUOTA) // LATE_ENTRY_DEDUCTION_INTERVAL
		if lwp_needed <= 0:
			continue

		marker = f"[AUTO-LATE-DEDUCT:{month_key}]"

		existing = frappe.db.count(
			"Leave Application",
			filters={
				"employee": row.employee,
				"description": ["like", f"%{marker}%"],
				"docstatus": 1,  # only count successfully submitted ones
			},
		)

		to_create = lwp_needed - existing
		if to_create <= 0:
			continue

		frappe.logger().info(
			f"[Late Entry Deduction] {row.employee_name} ({row.employee}): "
			f"{row.late_count} late entries → {lwp_needed} LWP needed, "
			f"{existing} already done, creating {to_create}"
		)

		for _ in range(to_create):
			try:
				_create_lwp_application(
					employee=row.employee,
					deduction_date=to_date,
					month_key=month_key,
					marker=marker,
					late_count=row.late_count,
				)
				frappe.db.commit()
			except Exception:
				frappe.log_error(
					frappe.get_traceback(),
					f"Late Entry Leave Deduction Failed: {row.employee} ({month_key})",
				)
				frappe.db.rollback()


def _create_lwp_application(employee, deduction_date, month_key, marker, late_count):
	"""Create and submit a Leave Without Pay application for late-entry deduction."""
	la = frappe.new_doc("Leave Application")
	la.employee = employee
	la.leave_type = "Leave Without Pay"
	la.from_date = deduction_date
	la.to_date = deduction_date
	la.total_leave_days = 1
	la.status = "Approved"
	la.description = (
		f"{marker} Leave Without Pay auto-applied for excessive late entries in {month_key}. "
		f"Employee had {late_count} late entries "
		f"(first {LATE_ENTRY_FREE_QUOTA} are free; "
		f"1 LWP added per {LATE_ENTRY_DEDUCTION_INTERVAL} late entries thereafter)."
	)
	la.flags.ignore_permissions = True
	la.flags.ignore_validate = True
	la.insert()
	# in_patch suppresses the "Holiday List not set" error in create_leave_ledger_entry
	# since this is a system-automated deduction, not a user-initiated leave request
	_prev = frappe.flags.in_patch
	frappe.flags.in_patch = True
	try:
		la.submit()
	finally:
		frappe.flags.in_patch = _prev
