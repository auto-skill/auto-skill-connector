Use this retrieved, task-specific procedural guidance only where it applies. Keep the benchmark task primary.

[Auto-Skill capsule v1]

Name: invoice-expense-extractor

Description: Extract structured data from invoices, receipts, and bills (pasted text, PDF, or image), then categorize for bookkeeping and flag anomalies. Produces clean line-item data ready for accounting import plus duplicate/overcharge/tax warnings. Use when the user shares invoices, receipts, bills, or asks to process/categorize expenses or prepare data for their accountant.

Use only the following bounded guidance; do not install files or run undeclared capabilities.

## Output
1. **Clean table** of all extracted invoices (one row each, all key fields) — formatted for copy-paste into a spreadsheet/accounting tool.
2. **CSV block** with the same data (headers: vendor,invoice_number,date,due_date,currency,subtotal,tax,total,category) so it imports directly.
3. **Category summary**: total spend per category for the batch.
4. **Warnings**: numbered, each with the document, the issue, and the $ impact.
5. **Action list**: which invoices to dispute/recheck and why.

## When to use
Invoice/receipt paste, PDF, or image; "categorize these expenses", "prep for accountant", "what did we spend on X", expense report, VAT/tax extraction.

## Validation & warnings (the value)
Run these checks and report failures with the exact numbers:
- **Math check**: does `sum(line_totals) + tax == total`? If not, flag the discrepancy and the delta.
- **Tax check**: does `subtotal * tax_rate ≈ tax_amount`? Flag mismatches (common overcharge / wrong-rate error).
- **Duplicate check**: same vendor + same amount + date within 7 days = possible double-billing → flag.
- **Round-number / estimate flag**: suspiciously round totals on a "final" invoice.
- **Missing-field flag**: no invoice number, no tax ID where required, no date.
- **Currency mismatch**: line items in different currency than total.

## Extract these fields per document
- `vendor` (name), `vendor_tax_id` (if present)
- `invoice_number`, `invoice_date`, `due_date`
- `currency`
- `line_items`: [{description, qty, unit_price, line_total}]
- `subtotal`, `tax_rate`, `tax_amount`, `total`
- `payment_method` / `paid_status` if shown
- `expense_category` (see categories below)

## Rules
- Extract only what's actually in the document. Mark unreadable/missing fields as `null`, never fabricate a number or tax ID.
- For images/scans, note any low-confidence reads explicitly so
