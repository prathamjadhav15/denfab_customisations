import frappe
from frappe.utils import getdate, get_first_day, today

# --- Configuration ---
LATE_ENTRY_LEAVE_TYPE = "Casual Leave"  # Leave type to deduct from
LATE_ENTRY_FREE_QUOTA = 3               # First N late entries are acceptable
LATE_ENTRY_DEDUCTION_INTERVAL = 3       # Deduct 1 leave per N late entries after quota
# ----------------------


def deduct_leave_for_late_entries():
	"""
	Daily scheduler (4 AM): Deduct leaves from Leave Allocation for excessive late entries.

	Rule:
	  - First 3 late entries in the month are free.
	  - For every 3rd late entry beyond that, deduct 1 leave day.

	Examples:
	  3 late entries  → 0 deductions
	  4, 5 late       → 0 deductions  (not yet 3 after quota)
	  6 late          → 1 deduction
	  9 late          → 2 deductions
	  12 late         → 3 deductions
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

		deductions_needed = (row.late_count - LATE_ENTRY_FREE_QUOTA) // LATE_ENTRY_DEDUCTION_INTERVAL
		if deductions_needed <= 0:
			continue

		# Count auto-deduction ledger entries already created for this employee this month.
		# These are negative, non-expiry entries against Leave Allocation — not created in
		# normal ERPNext flow — so counting them is a reliable idempotency guard.
		existing_deductions = frappe.db.count(
			"Leave Ledger Entry",
			filters={
				"employee": row.employee,
				"leave_type": LATE_ENTRY_LEAVE_TYPE,
				"transaction_type": "Leave Allocation",
				"leaves": ["<", 0],
				"is_expired": 0,
				"from_date": ["between", [from_date, to_date]],
				"docstatus": 1,
			},
		)

		leaves_to_deduct = deductions_needed - existing_deductions
		if leaves_to_deduct <= 0:
			continue

		frappe.logger().info(
			f"[Late Entry Deduction] {row.employee_name} ({row.employee}): "
			f"{row.late_count} late entries → {deductions_needed} deductions needed, "
			f"{existing_deductions} already done, deducting {leaves_to_deduct}"
		)

		for _ in range(leaves_to_deduct):
			try:
				_deduct_from_leave_allocation(
					employee=row.employee,
					employee_name=row.employee_name,
					deduction_date=to_date,
					month_key=month_key,
				)
				frappe.db.commit()
			except Exception:
				frappe.log_error(
					frappe.get_traceback(),
					f"Late Entry Leave Deduction Failed: {row.employee} ({month_key})",
				)
				frappe.db.rollback()


def _deduct_from_leave_allocation(employee, employee_name, deduction_date, month_key):
	"""Deduct 1 leave directly from the active Leave Allocation via Leave Ledger Entry."""
	allocation = frappe.db.get_value(
		"Leave Allocation",
		filters={
			"employee": employee,
			"leave_type": LATE_ENTRY_LEAVE_TYPE,
			"from_date": ["<=", deduction_date],
			"to_date": [">=", deduction_date],
			"docstatus": 1,
		},
		fieldname=["name", "employee_name"],
		as_dict=True,
	)

	if not allocation:
		frappe.log_error(
			f"No active '{LATE_ENTRY_LEAVE_TYPE}' allocation found for {employee} on {deduction_date}. "
			f"Could not deduct leave for late entries in {month_key}.",
			"Late Entry Leave Deduction",
		)
		return

	ledger = frappe.new_doc("Leave Ledger Entry")
	ledger.employee = employee
	ledger.employee_name = allocation.employee_name or employee_name
	ledger.leave_type = LATE_ENTRY_LEAVE_TYPE
	ledger.transaction_type = "Leave Allocation"
	ledger.transaction_name = allocation.name
	ledger.from_date = deduction_date
	ledger.to_date = deduction_date
	ledger.leaves = -1
	ledger.is_carry_forward = 0
	ledger.is_expired = 0
	ledger.is_lwp = 0
	ledger.flags.ignore_permissions = True
	ledger.submit()

	frappe.logger().info(
		f"[Late Entry Deduction] Deducted 1 leave from allocation {allocation.name} "
		f"for {employee} ({month_key})"
	)
