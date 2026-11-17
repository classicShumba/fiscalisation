from datetime import datetime, timedelta
import json
import logging
import base64

import requests
from markupsafe import Markup, escape

from odoo import models, fields, api, _, SUPERUSER_ID
from odoo.exceptions import UserError
from odoo.modules import module as odoo_module

_logger = logging.getLogger(__name__)

FISCAL_STATES = [
    ('not_applicable', 'Not Applicable'),
    ('awaiting_payment', 'Awaiting Payment'),
    ('pending', 'Queued'),
    ('submitting', 'Submitting'),
    ('done', 'Fiscalised'),
    ('failed', 'Failed'),
    ('blocked', 'Blocked'),
    ('needs_check', 'Needs Check'),
]
# Payment states that make a customer invoice eligible for fiscalisation.
FISCAL_PAID_STATES = ('partial', 'in_payment', 'paid')
# States in which the document is (or may already be) registered with FDMS.
FISCAL_LOCKED_STATES = ('submitting', 'done', 'needs_check')
# States needing human attention.
FISCAL_ATTENTION_STATES = ('failed', 'blocked', 'needs_check')
# Back-off between automatic retries of transient errors (minutes).
FISCAL_RETRY_DELAYS = (5, 15, 60, 180, 360)
FISCAL_MAX_ATTEMPTS = 6
# A document left in 'submitting' longer than this is considered stuck.
FISCAL_STUCK_MINUTES = 15
# Error codes treated as transient (retry with back-off).
FISCAL_TRANSIENT_CODES = {
    'CONNECTION_REFUSED', 'TIMEOUT', 'CONNECTION_FAILED',
    'INVALID_RESPONSE', 'UNKNOWN_ERROR', 'UNKNOWN',
}
FISCAL_DUPLICATE_CODE = 'RCPT013'


class AccountMove(models.Model):
    _inherit = 'account.move'

    customer_vat = fields.Char(string='Customer VAT', related='partner_id.vat', readonly=True, store=True)
    customer_tin = fields.Char(string='Customer TIN', related='partner_id.tin', readonly=True, store=True)
    receipt_type = fields.Char(string='Receipt Type', compute='_compute_receipt_type')
    qr_url = fields.Char(string='QR Code URL', readonly=True, copy=False)
    qr_code = fields.Binary(string='QR Code', compute='_compute_qr_code', copy=False)
    fdms_url = fields.Char(string='FDMS URL', readonly=True, copy=False, compute='_compute_fdms_url')
    fiscal_date = fields.Datetime(string='Fiscalisation Date', readonly=True, copy=False)
    device_id = fields.Char(string='Device ID', readonly=True, compute='_compute_device_id', store=True)
    device_serial = fields.Char(string='Device Serial', readonly=True, compute='_compute_device_serial', store=True)
    receipt_global_number = fields.Char(string='Receipt Global Number', readonly=True, copy=False)
    receipt_number = fields.Char(string='Receipt Number', readonly=True, copy=False)
    fiscal_day_no = fields.Char(string='Fiscal Day', readonly=True, copy=False)
    verification_code = fields.Char(string='Verification Code', readonly=True, copy=False)
    fiscalised = fields.Boolean(string='Fiscalised', readonly=True, default=False, copy=False)

    # Automatic fiscalisation queue
    fiscal_state = fields.Selection(
        FISCAL_STATES, string='Fiscal Status', default='not_applicable',
        readonly=True, copy=False, index=True, tracking=True,
        help="Not Applicable: not a customer invoice/credit note, or issued before automatic fiscalisation.\n"
             "Awaiting Payment: confirmed; fiscalised automatically once a payment is linked.\n"
             "Queued: will be submitted to FDMS by the fiscalisation job.\n"
             "Submitting: submission in progress.\n"
             "Fiscalised: accepted by FDMS.\n"
             "Failed: connection errors persisted after all automatic retries.\n"
             "Blocked: data must be corrected before it can be fiscalised.\n"
             "Needs Check: FDMS may already hold this document; verify before retrying.")
    fiscal_attempts = fields.Integer(string='Fiscal Attempts', readonly=True, copy=False)
    fiscal_next_attempt = fields.Datetime(string='Next Fiscal Attempt', readonly=True, copy=False)
    fiscal_submitted_at = fields.Datetime(string='Fiscal Submission Started', readonly=True, copy=False)
    fiscal_last_error = fields.Text(string='Last Fiscal Error', readonly=True, copy=False)
    fiscal_issues = fields.Text(string='Fiscal Pre-check Issues', readonly=True, copy=False)

    _fiscal_receipt_unique = models.UniqueIndex(
        "(company_id, device_id, receipt_global_number) WHERE receipt_global_number IS NOT NULL AND receipt_global_number <> ''",
        "This FDMS receipt global number is already linked to another document.",
    )

    @api.depends('move_type', 'line_ids.partner_id')
    def _compute_customer_vat(self):
        for invoice in self:
            partner = invoice.partner_id or (invoice.line_ids.partner_id[0] if invoice.line_ids.partner_id else None)
            invoice.customer_vat = partner.vat or '' if partner else ''

    @api.depends('move_type', 'line_ids.partner_id')
    def _compute_customer_tin(self):
        for invoice in self:
            partner = invoice.partner_id or (invoice.line_ids.partner_id[0] if invoice.line_ids.partner_id else None)
            invoice.customer_tin = partner.tin or '' if partner else ''

    @api.depends('move_type')
    def _compute_receipt_type(self):
        for invoice in self:
            if invoice.move_type == 'out_refund':
                invoice.receipt_type = 'CreditNote'
            elif invoice.move_type == 'in_refund':
                invoice.receipt_type = 'DebitNote'
            else:
                invoice.receipt_type = 'FiscalInvoice'

    @api.depends('company_id')
    def _compute_device_id(self):
        for invoice in self:
            device = invoice._fiscal_get_device()
            invoice.device_id = device.device_id if device else False

    @api.depends('company_id')
    def _compute_device_serial(self):
        for invoice in self:
            device = invoice._fiscal_get_device()
            invoice.device_serial = device.device_serial if device else False

    @api.depends('company_id')
    def _compute_fdms_url(self):
        for invoice in self:
            device = invoice._fiscal_get_device()
            invoice.fdms_url = device.fdms_url if device else False

    def _compute_qr_code(self):
        for invoice in self:
            if invoice.verification_code:
                invoice.qr_code = self._generate_fiscal_invoice_qr_code(invoice.qr_url)
            else:
                invoice.qr_code = False

    def _generate_fiscal_invoice_qr_code(self, data):
        try:
            import qrcode
            from io import BytesIO
            qr = qrcode.QRCode(
                version=1,
                error_correction=qrcode.constants.ERROR_CORRECT_L,
                box_size=10,
                border=4,
            )
            qr.add_data(data)
            qr.make(fit=True)
            img = qr.make_image(fill_color="black", back_color="white")
            buffer = BytesIO()
            img.save(buffer, format="PNG")
            return base64.b64encode(buffer.getvalue())
        except ImportError:
            _logger.warning("QR code generation requires python-qrcode library")
            return False

    # -------------------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------------------

    def _fiscal_get_device(self):
        self.ensure_one()
        return self.env['fiscal.device'].sudo().search([
            ('company_id', '=', self.company_id.id)
        ], limit=1)

    @api.model
    def _fiscal_commit(self):
        """Commit the current transaction, except while running tests."""
        if not odoo_module.current_test:
            self.env.cr.commit()

    @api.model
    def _fiscal_trigger_cron(self):
        cron = self.env.ref('fiscalisation.ir_cron_fiscalise_queue', raise_if_not_found=False)
        if cron:
            cron.sudo()._trigger()

    def _fiscal_preflight(self):
        """Return a newline separated list of problems that would prevent fiscalisation."""
        self.ensure_one()
        issues = []
        if not self._fiscal_get_device():
            issues.append(_("No fiscal device is configured for company %s.", self.company_id.name))
        if not self.partner_id:
            issues.append(_("A customer is required."))
        product_lines = self.invoice_line_ids.filtered(
            lambda l: l.display_type in (False, 'product') and l.quantity > 0)
        for line in product_lines:
            if not line.product_id:
                issues.append(_("Line '%s' has no product.", (line.name or '')[:60]))
            elif not line.product_id.hs_code:
                issues.append(_("Product '%s' has no HS Code.", line.product_id.display_name))
        inclusion = {line.tax_ids[0].price_include for line in product_lines if line.tax_ids}
        if len(inclusion) > 1:
            issues.append(_("Lines mix tax-inclusive and tax-exclusive prices."))
        if self.move_type == 'out_refund' and not self.reversed_entry_id.fiscalised:
            issues.append(_("A credit note can only be fiscalised against a fiscalised original invoice."))
        return "\n".join(issues)

    def _fiscal_alert(self, summary, note, extra_users=None):
        """Schedule a to-do for the device's alert recipients, or fiscalisation admins if none are set."""
        self.ensure_one()
        users = self._fiscal_get_device().alert_user_ids
        if not users:
            group = self.env.ref('fiscalisation.admin_fiscalisation', raise_if_not_found=False)
            users = group.sudo().user_ids if group else self.env['res.users']
        if extra_users:
            users |= extra_users
        users = users.filtered(lambda u: u.active and not u.share and u.id != SUPERUSER_ID)
        open_activities = self.sudo().activity_ids.filtered(lambda a: a.summary == summary)
        for user in users - open_activities.user_id:
            try:
                self.sudo().activity_schedule(
                    'mail.mail_activity_data_todo', summary=summary, note=note, user_id=user.id)
            except Exception:  # never let alerting break the queue
                _logger.exception("Could not schedule fiscal alert for %s", self.name)

    def _fiscal_log(self, title, detail=None):
        body = Markup("<b>%s</b>") % title
        if detail:
            body += Markup("<br/>") + Markup("<br/>").join(escape(l) for l in str(detail).splitlines())
        self.sudo().message_post(body=body, message_type='comment', subtype_xmlid='mail.mt_note')

    # -------------------------------------------------------------------------
    # Queueing
    # -------------------------------------------------------------------------

    def _post(self, soft=True):
        posted = super()._post(soft=soft)
        posted._fiscal_queue_on_post()
        return posted

    def action_post(self):
        if self.env.context.get('fiscal_preflight_strict'):
            problems = []
            for move in self.filtered(lambda m: m.move_type == 'out_invoice' and m._fiscal_get_device()):
                issues = move._fiscal_preflight()
                if issues:
                    problems.append("%s:\n%s" % (move.display_name, issues))
            if problems:
                raise UserError(_("This invoice cannot be fiscalised as it stands. Fix the following before confirming:\n\n%s")
                                % "\n\n".join(problems))
        return super().action_post()

    def _fiscal_queue_on_post(self):
        trigger = False
        for move in self:
            if move.move_type not in ('out_invoice', 'out_refund'):
                continue
            if move.fiscalised or move.fiscal_state in FISCAL_LOCKED_STATES:
                continue
            if not move._fiscal_get_device():
                continue
            reset = {'fiscal_attempts': 0, 'fiscal_next_attempt': False, 'fiscal_last_error': False}
            if move.move_type == 'out_invoice':
                move.write(dict(reset, fiscal_state='awaiting_payment', fiscal_issues=move._fiscal_preflight() or False))
                if move.payment_state in FISCAL_PAID_STATES:
                    trigger = True
            elif move.reversed_entry_id.fiscalised:
                move.write(dict(reset, fiscal_state='pending', fiscal_issues=move._fiscal_preflight() or False))
                trigger = True
            else:
                reason = _("Credit note does not reverse a fiscalised invoice; it was not sent to FDMS.")
                move.write(dict(reset, fiscal_state='blocked', fiscal_issues=reason, fiscal_last_error=reason))
                move._fiscal_alert(_("Fiscalisation blocked"), reason)
        if trigger:
            self._fiscal_trigger_cron()

    def _invoice_paid_hook(self):
        res = super()._invoice_paid_hook()
        if self.filtered(lambda m: m.move_type == 'out_invoice' and m.fiscal_state == 'awaiting_payment'):
            self._fiscal_trigger_cron()
        return res

    # -------------------------------------------------------------------------
    # Locks
    # -------------------------------------------------------------------------

    def _fiscal_check_not_locked(self):
        locked = self.filtered(lambda m: m.fiscalised or m.fiscal_state in FISCAL_LOCKED_STATES)
        if locked:
            raise UserError(_(
                "%s has been (or may have been) fiscalised with ZIMRA and cannot be reset or cancelled. "
                "Correct it with a credit or debit note instead.", ", ".join(locked.mapped('name'))))

    def button_draft(self):
        self._fiscal_check_not_locked()
        res = super().button_draft()
        self.filtered(lambda m: m.fiscal_state in ('awaiting_payment', 'pending', 'failed', 'blocked')).write({
            'fiscal_state': 'not_applicable', 'fiscal_next_attempt': False,
        })
        return res

    def button_cancel(self):
        self._fiscal_check_not_locked()
        res = super().button_cancel()
        self.filtered(lambda m: m.fiscal_state in ('awaiting_payment', 'pending', 'failed', 'blocked')).write({
            'fiscal_state': 'not_applicable', 'fiscal_next_attempt': False,
        })
        return res

    # -------------------------------------------------------------------------
    # User actions
    # -------------------------------------------------------------------------

    def action_fiscalise_invoice(self):
        """Queue the document for immediate fiscalisation (manual path)."""
        for move in self:
            if move.fiscalised or move.fiscal_state in FISCAL_LOCKED_STATES:
                raise UserError(_("%s is already fiscalised or being fiscalised.", move.name))
            if move.state != 'posted' or move.move_type not in ('out_invoice', 'out_refund'):
                raise UserError(_("Only confirmed customer invoices and credit notes can be fiscalised."))
            if not move._fiscal_get_device():
                raise UserError(_("No fiscal device configured for this company"))
        self.write({
            'fiscal_state': 'pending', 'fiscal_attempts': 0,
            'fiscal_next_attempt': False, 'fiscal_last_error': False,
        })
        self._fiscal_trigger_cron()
        return self._fiscal_notify(_("Queued for fiscalisation"),
                                   _("The document will be submitted to ZIMRA within a minute. Refresh to see the result."))

    def action_fiscal_retry(self):
        """Admin: put failed / blocked / needs-check documents back in the queue."""
        if not self.env.user.has_group('fiscalisation.admin_fiscalisation'):
            raise UserError(_("Only Fiscalisation Administrators can retry fiscalisation."))
        moves = self.filtered(lambda m: m.fiscal_state in FISCAL_ATTENTION_STATES and not m.fiscalised)
        for move in moves:
            move._fiscal_log(_("Fiscalisation retry requested by %s", self.env.user.name))
        moves.write({
            'fiscal_state': 'pending', 'fiscal_attempts': 0,
            'fiscal_next_attempt': False, 'fiscal_submitted_at': False,
        })
        moves.activity_ids.filtered(lambda a: (a.summary or '').startswith('Fiscalisation')).action_done()
        self._fiscal_trigger_cron()
        return self._fiscal_notify(_("Queued for fiscalisation"), _("%s document(s) queued.", len(moves)))

    def _fiscal_notify(self, title, message, kind='info'):
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {'title': title, 'message': message, 'type': kind, 'sticky': False,
                       'next': {'type': 'ir.actions.client', 'tag': 'soft_reload'}},
        }

    # -------------------------------------------------------------------------
    # Queue processing (cron)
    # -------------------------------------------------------------------------

    @api.model
    def _fiscal_queue_domain(self):
        now = fields.Datetime.now()
        return [
            ('state', '=', 'posted'),
            ('move_type', 'in', ('out_invoice', 'out_refund')),
            ('fiscalised', '=', False),
            '|', ('fiscal_next_attempt', '=', False), ('fiscal_next_attempt', '<=', now),
            '|', ('fiscal_state', '=', 'pending'),
            '&', '&', ('fiscal_state', '=', 'awaiting_payment'),
            ('move_type', '=', 'out_invoice'),
            ('payment_state', 'in', FISCAL_PAID_STATES),
        ]

    @api.model
    def _cron_fiscalise_queue(self, limit=100):
        self._fiscal_recover_stuck()
        for move_id in self.search(self._fiscal_queue_domain(), limit=limit, order='id').ids:
            move = self.browse(move_id)
            if not move._fiscal_lock():
                continue
            try:
                move._fiscal_process_one()
            except Exception:
                _logger.exception("Unexpected error fiscalising move %s", move_id)
                if odoo_module.current_test:
                    raise
                # Anything committed as 'submitting' is recovered as 'needs_check' by _fiscal_recover_stuck.
                self.env.cr.rollback()
                continue
            self._fiscal_commit()

    @api.model
    def _fiscal_recover_stuck(self):
        limit = fields.Datetime.now() - timedelta(minutes=FISCAL_STUCK_MINUTES)
        stuck = self.search([('fiscal_state', '=', 'submitting'), ('fiscal_submitted_at', '<', limit)])
        for move in stuck:
            reason = _("Submission did not finish. FDMS may already hold this document - "
                       "verify on FDMS before retrying, otherwise a duplicate receipt may be issued.")
            move.write({'fiscal_state': 'needs_check', 'fiscal_last_error': reason})
            move._fiscal_log(_("Fiscalisation needs checking"), reason)
            move._fiscal_alert(_("Fiscalisation needs checking"), reason)
        if stuck:
            self._fiscal_commit()

    def _fiscal_lock(self):
        """Row-lock the move if still eligible; False if another worker has it."""
        self.ensure_one()
        self.env.cr.execute("""
            SELECT id FROM account_move
             WHERE id = %s AND state = 'posted' AND NOT COALESCE(fiscalised, FALSE)
               AND fiscal_state IN ('awaiting_payment', 'pending')
               FOR UPDATE SKIP LOCKED
        """, [self.id])
        locked = bool(self.env.cr.fetchone())
        self.invalidate_recordset()
        return locked

    def _fiscal_process_one(self):
        self.ensure_one()
        device = self._fiscal_get_device()
        if not device:
            return self._fiscal_set_blocked(_("No fiscal device configured for this company."))
        if self.receipt_global_number:
            # Defensive: receipt data already stored, never submit twice.
            return self.write({'fiscal_state': 'done', 'fiscalised': True})
        if not device.is_day_open:
            # Fiscal day closed (e.g. during the night close/open window): wait without burning an attempt.
            return self.write({'fiscal_next_attempt': fields.Datetime.now() + timedelta(minutes=5),
                               'fiscal_last_error': _("Fiscal day is closed; waiting for it to open.")})

        issues = self._fiscal_preflight()
        if issues:
            return self._fiscal_set_blocked(issues)
        try:
            payload = self._prepare_fiscal_payload()
        except UserError as e:
            return self._fiscal_set_blocked(str(e))

        # Persist the in-flight state before calling FDMS so a crash can never cause a silent resubmission.
        self.write({
            'fiscal_state': 'submitting',
            'fiscal_submitted_at': fields.Datetime.now(),
            'fiscal_attempts': self.fiscal_attempts + 1,
            'fiscal_issues': False,
        })
        self._fiscal_commit()

        started = fields.Datetime.now().replace(microsecond=0)
        _logger.info("Fiscalising %s (attempt %s)", self.name, self.fiscal_attempts)
        try:
            response = device._api_request('/api/v1/receipts', payload=payload)
        except Exception as e:  # _api_request raises UserError for all request failures
            code, status = 'UNKNOWN', 0
            if device.last_status_check and device.last_status_check >= started:
                code, status = device.last_error_code or 'UNKNOWN', device.last_error_status or 0
            return self._fiscal_handle_error(code, status, str(e))

        try:
            self._process_fiscal_response(response)
        except Exception as e:
            reason = _("FDMS accepted the document but the response could not be stored (%s). "
                       "Raw response:\n%s") % (e, json.dumps(response, default=str)[:2000])
            self.write({'fiscal_state': 'needs_check', 'fiscal_last_error': reason})
            self._fiscal_log(_("Fiscalisation needs checking"), reason)
            self._fiscal_alert(_("Fiscalisation needs checking"), reason)
            return

        self.write({'fiscal_state': 'done', 'fiscal_next_attempt': False, 'fiscal_last_error': False})
        self._fiscal_log(_("Fiscalised with ZIMRA"), _(
            "Receipt %(rn)s (global %(gn)s), fiscal day %(day)s, verification code %(code)s",
            rn=self.receipt_number, gn=self.receipt_global_number,
            day=self.fiscal_day_no, code=self.verification_code))
        self._fiscal_commit()
        self._fiscal_after_done()

    def _fiscal_set_blocked(self, reason):
        self.write({'fiscal_state': 'blocked', 'fiscal_last_error': reason,
                    'fiscal_issues': reason, 'fiscal_next_attempt': False})
        self._fiscal_log(_("Fiscalisation blocked"), reason)
        self._fiscal_alert(_("Fiscalisation blocked"), reason, extra_users=self.invoice_user_id)

    def _fiscal_handle_error(self, code, status, message):
        self.ensure_one()
        if code == FISCAL_DUPLICATE_CODE:
            reason = _("FDMS reports this invoice number already exists (%(code)s). "
                       "Verify the receipt on FDMS and record it; it will not be resubmitted automatically.\n%(msg)s",
                       code=code, msg=message)
            self.write({'fiscal_state': 'needs_check', 'fiscal_last_error': reason})
            self._fiscal_log(_("Fiscalisation needs checking"), reason)
            self._fiscal_alert(_("Fiscalisation needs checking"), reason)
            return
        transient = code in FISCAL_TRANSIENT_CODES or not status or status >= 500 or status in (401, 403, 408, 429)
        if not transient:
            return self._fiscal_set_blocked(message)
        if self.fiscal_attempts >= FISCAL_MAX_ATTEMPTS:
            self.write({'fiscal_state': 'failed', 'fiscal_last_error': message, 'fiscal_next_attempt': False})
            self._fiscal_log(_("Fiscalisation failed after %s attempts", self.fiscal_attempts), message)
            self._fiscal_alert(_("Fiscalisation failed"), message)
            return
        delay = FISCAL_RETRY_DELAYS[min(self.fiscal_attempts - 1, len(FISCAL_RETRY_DELAYS) - 1)]
        self.write({
            'fiscal_state': 'pending',
            'fiscal_last_error': message,
            'fiscal_next_attempt': fields.Datetime.now() + timedelta(minutes=delay),
        })

    # -------------------------------------------------------------------------
    # Post-fiscalisation: fiscalised PDF
    # -------------------------------------------------------------------------

    def _fiscal_after_done(self):
        self.ensure_one()
        try:
            with self.env.cr.savepoint():
                self._fiscal_refresh_pdf()
        except Exception:
            _logger.exception("Could not regenerate fiscalised PDF for %s", self.name)
            return
        self._fiscal_commit()

    def _fiscal_refresh_pdf(self):
        """Replace the stored invoice PDF with one rendered after fiscalisation.

        Odoo freezes the PDF the first time an invoice is sent or printed; without this,
        downloads would keep serving the copy without fiscal details / QR code.
        """
        self.ensure_one()
        old = self.sudo().invoice_pdf_report_id
        if old:
            # Keep the superseded copy for audit, detached from the invoice PDF field.
            old.write({'res_field': False, 'name': _("%s (superseded, not fiscalised)", old.name)})
        self.invalidate_recordset(['invoice_pdf_report_id', 'invoice_pdf_report_file'])
        was_sent = self.is_move_sent
        send = self.env['account.move.send'].sudo()
        invoices_data = {self: {'pdf_report': send._get_default_pdf_report_id(self)}}
        send._prepare_invoice_pdf_report(invoices_data)
        send._link_invoice_documents(invoices_data)
        if self.is_move_sent != was_sent:
            self.is_move_sent = was_sent

    # -------------------------------------------------------------------------
    # FDMS payload / response
    # -------------------------------------------------------------------------

    def _prepare_fiscal_payload(self):
        
        lines = self.env['account.move.line'].search([
            ('move_id', '=', self.id),
            ('quantity', '>', 0)
        ])
        
        receipt_type = self.receipt_type
        
        tax_inclusion_types = set()
        for line in lines:
            if not line.tax_ids:
                continue
                # raise UserError(_(f"Product {line.product_id.name} has no tax configuration."))
            tax_inclusion_types.add(line.tax_ids[0].price_include)
            
        if len(tax_inclusion_types) > 1:
            raise UserError(_("Mixed tax inclusion types detected. All invoice lines must consistently have either tax-inclusive or tax-exclusive prices."))
            
        receiptLinesTaxInclusive = tax_inclusion_types.pop() if tax_inclusion_types else True
        
        return {
            "receipt": {
                "receiptType": self.receipt_type,
                "receiptCurrency": self.currency_id.name,
                "invoiceNo": self.name,
                "buyerData": self._prepare_buyer_data(),
                "receiptNotes": self.ref if self.ref else "",
                "creditDebitNoteInvoiceNo": self.reversed_entry_id.name if self.move_type in ('out_refund', 'in_refund') else "",
                "receiptLinesTaxInclusive": receiptLinesTaxInclusive,
                "receiptLines": self._prepare_receipt_lines(),
                "receiptPayments": self._prepare_payment_data(),
                "receiptTotal": self._adjust_amount(self.amount_total, receipt_type),
                "receiptPrintForm": "InvoiceA4"
            }
        }

    def _prepare_buyer_data(self):
        partner = self.partner_id
        if not partner:
            raise UserError(_("Customer information is required for fiscalisation"))
            
        if partner.tin:
            buyer_data = {
                "buyerRegisterName": partner.name,
                "buyerTradeName": partner.commercial_partner_id.name,
                "vatNumber": self.customer_vat or self.partner_id.vat,
                "buyerTIN": self.customer_tin or self.partner_id.tin,
                "buyerContacts": {
                    "phoneNo": partner.phone or "",
                    "email": partner.email or ""
                },
                "buyerAddress": {
                    "province": partner.state_id.name or "",
                    "city": partner.city or "",
                    "street": partner.street or "",
                    "houseNo": partner.street2 or "",
                    "district": ""
                }
            }
        else:
            buyer_data = None
        
        return buyer_data 

    def _prepare_receipt_lines(self):
        inv_lines = self.env['account.move.line'].search([
            ('move_id', '=', self.id),
            ('quantity', '>', 0),
            ('display_type', 'in', (False, 'product')),  # Only product lines, not section/notes
            ('product_id', '!=', False)                  # Must have an associated product
        ])
        receipt_type = self.receipt_type
        lines = []
        for line in inv_lines:
            # Ensure HS Code is present
            if not line.product_id.hs_code:
                raise UserError(_(f'Product ({line.name}) has no HS Code'))
            
            # Always add a non-discounted sale line
            sale_line_data = {
                "receiptLineType": "Sale",
                "receiptLineNo": len(lines) + 1,
                "receiptLineHSCode": line.product_id.hs_code,
                "receiptLineName": line.name[:100],
                "receiptLinePrice": self._adjust_amount(line.price_unit, receipt_type),
                "receiptLineQuantity": float(line.quantity),
                "receiptLineTotal": self._adjust_amount(line.price_unit * line.quantity, receipt_type),
            }
            if line.tax_ids:
                # Sum tax amounts from all applicable taxes
                sale_line_data["taxPercent"] = float(sum(line.tax_ids.mapped("amount")))
            lines.append(sale_line_data)

            # If the line has a discount, add a separate discount line
            if line.discount > 0:
                discount_amount = line.price_unit * (line.discount / 100.0)
                discount_total = discount_amount * line.quantity
                discount_line_data = {
                    "receiptLineType": "Discount",
                    "receiptLineNo": len(lines) + 1,
                    "receiptLineHSCode": line.product_id.hs_code,
                    "receiptLineName": sale_line_data["receiptLineName"] + " (Discount)",
                    "receiptLinePrice": -self._adjust_amount(discount_amount, receipt_type),
                    "receiptLineQuantity": float(line.quantity),
                    "receiptLineTotal": -self._adjust_amount(discount_total, receipt_type),
                }
                
                # Include tax information for consistent tax calculations
                if "taxPercent" in sale_line_data:
                    discount_line_data["taxPercent"] = sale_line_data["taxPercent"]
                
                lines.append(discount_line_data)
        return lines

    def _prepare_payment_data(self):
        PAYMENT_TYPE_MAPPING = {
            'cash': 'Cash',
            'card': 'Card',
            'bank': 'Transfer',
            'transfer': 'Transfer'
        }
        
        receipt_type = self.receipt_type
        payments = []
        payments.append({
            "moneyTypeCode": 'Cash',
            "paymentAmount": self._adjust_amount(self.amount_total, receipt_type)
        })
        
        return payments

    def _process_fiscal_response(self, response):
        try:
            receipt_date = response.get('receiptFiscalDate')
            fiscal_date = fields.Datetime.now()
            
            if receipt_date:
                try:
                    # Handle ISO format with optional milliseconds
                    receipt_date = receipt_date.split('.')[0]  # Remove milliseconds if present
                    dt = datetime.strptime(receipt_date, "%Y-%m-%dT%H:%M:%S")
                    fiscal_date = fields.Datetime.to_string(dt)
                except ValueError as e:
                    _logger.warning("Date format error: %s, using current date", str(e))
                    
            self.write({
                # 'fiscal_signature': response.get('verificationCode'),
                # 'qr_code': self._generate_qr_code(response.get('qrCodeUrl')),
                'qr_url': response.get('qrCodeUrl'),
                'fiscal_date': fiscal_date,
                'device_id': response.get('deviceID'),
                'receipt_global_number': response.get('receiptGlobalNo'),
                'receipt_number': response.get('receiptNumber'),
                'fiscal_day_no': response.get('fiscalDayNo'),
                'verification_code': response.get('verificationCode'),
                'fiscalised': True
            })
            
        except KeyError as e:
            _logger.error("Missing expected field in API response: %s", str(e))
            raise UserError(_("Invalid response from fiscal device")) from e

    def _adjust_amount(self, amount, receipt_type):
        """Adjust amount sign based on receipt type"""
        multiplier = -1 if receipt_type.upper() == 'CREDITNOTE' else 1
        return float(amount) * multiplier
