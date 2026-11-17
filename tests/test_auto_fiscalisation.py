from datetime import timedelta
from unittest.mock import patch

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests import tagged

from odoo.addons.account.tests.common import AccountTestInvoicingCommon

DEVICE_CLS = 'odoo.addons.fiscalisation.models.fiscal_device.FiscalDevice'


def ok_response(n=1):
    return {
        'qrCodeUrl': 'https://fdmstest.zimra.co.zw/check/%s' % n,
        'receiptFiscalDate': '2026-11-16T10:00:00',
        'deviceID': '12345',
        'receiptGlobalNo': str(1000 + n),
        'receiptNumber': str(n),
        'fiscalDayNo': '7',
        'verificationCode': 'ABCD-%04d' % n,
    }


@tagged('post_install', '-at_install', 'fiscalisation')
class TestAutoFiscalisation(AccountTestInvoicingCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.device = cls.env['fiscal.device'].sudo().create({
            'name': 'Test Device',
            'company_id': cls.env.company.id,
            'device_id': 12345,
            'device_serial': 'TEST-SERIAL',
            'activation_key': 'KEY',
            'base_url': 'https://fisc.example.invalid',
            'fiscal_day_status': 'FISCALDAYOPENED',
        })
        cls.product_a.hs_code = '85176200'
        cls.Move = cls.env['account.move']

    # helpers -----------------------------------------------------------------

    def _invoice(self, post=True, **kw):
        inv = self.init_invoice('out_invoice', partner=self.partner_a, products=self.product_a,
                                post=post, **kw)
        return inv

    def _pay(self, invoice, amount=None):
        self.env['account.payment.register'].with_context(
            active_model='account.move', active_ids=invoice.ids,
        ).create({'amount': amount or invoice.amount_total})._create_payments()

    def _run_queue(self, side_effect=None, return_value=None):
        with patch(DEVICE_CLS + '._api_request', autospec=True,
                   side_effect=side_effect, return_value=return_value) as api, \
             patch('odoo.addons.fiscalisation.models.account_move.AccountMove._fiscal_refresh_pdf'):
            self.Move._cron_fiscalise_queue()
        return api

    def _api_error(self, code, status):
        def raiser(device, *args, **kwargs):
            device._log_error_details({'code': code, 'message': code, 'operation_id': '', 'status': status})
            raise UserError("FDMS Error [%s]" % code)
        return raiser

    # tests -------------------------------------------------------------------

    def test_draft_is_never_queued(self):
        inv = self._invoice(post=False)
        self.assertEqual(inv.fiscal_state, 'not_applicable')
        api = self._run_queue(return_value=ok_response())
        api.assert_not_called()

    def test_post_waits_for_payment(self):
        inv = self._invoice()
        self.assertEqual(inv.fiscal_state, 'awaiting_payment')
        api = self._run_queue(return_value=ok_response())
        api.assert_not_called()

    def test_payment_triggers_fiscalisation(self):
        inv = self._invoice()
        self._pay(inv, amount=inv.amount_total / 2)  # partial payment also qualifies
        self.assertIn(inv.payment_state, ('partial', 'in_payment', 'paid'))
        api = self._run_queue(return_value=ok_response())
        self.assertEqual(api.call_count, 1)
        self.assertEqual(inv.fiscal_state, 'done')
        self.assertTrue(inv.fiscalised)
        self.assertEqual(inv.receipt_global_number, '1001')
        self.assertEqual(inv.verification_code, 'ABCD-0001')
        # never submitted twice
        api = self._run_queue(return_value=ok_response(2))
        api.assert_not_called()

    def test_credit_note_on_fiscalised_invoice(self):
        inv = self._invoice()
        self._pay(inv)
        self._run_queue(return_value=ok_response(1))
        self.assertTrue(inv.fiscalised)
        refund = inv._reverse_moves([{'ref': 'refund'}])
        refund.action_post()
        self.assertEqual(refund.fiscal_state, 'pending')
        api = self._run_queue(return_value=ok_response(2))
        payload = api.call_args.kwargs['payload']['receipt']
        self.assertEqual(payload['receiptType'], 'CreditNote')
        self.assertEqual(payload['creditDebitNoteInvoiceNo'], inv.name)
        self.assertEqual(refund.fiscal_state, 'done')

    def test_credit_note_on_unfiscalised_invoice_is_blocked(self):
        inv = self._invoice()
        refund = inv._reverse_moves([{'ref': 'refund'}])
        refund.action_post()
        self.assertEqual(refund.fiscal_state, 'blocked')

    def test_transient_error_backs_off_then_fails(self):
        inv = self._invoice()
        self._pay(inv)
        for attempt in range(1, 7):
            self._run_queue(side_effect=self._api_error('TIMEOUT', 0))
            if attempt < 6:
                self.assertEqual(inv.fiscal_state, 'pending')
                self.assertTrue(inv.fiscal_next_attempt)
                inv.fiscal_next_attempt = fields.Datetime.now() - timedelta(seconds=1)
        self.assertEqual(inv.fiscal_state, 'failed')
        self.assertEqual(inv.fiscal_attempts, 6)
        self.assertFalse(inv.fiscalised)

    def test_validation_error_blocks(self):
        inv = self._invoice()
        self._pay(inv)
        self._run_queue(side_effect=self._api_error('RCPT020', 422))
        self.assertEqual(inv.fiscal_state, 'blocked')

    def test_duplicate_needs_check(self):
        inv = self._invoice()
        self._pay(inv)
        self._run_queue(side_effect=self._api_error('RCPT013', 400))
        self.assertEqual(inv.fiscal_state, 'needs_check')
        api = self._run_queue(return_value=ok_response())
        api.assert_not_called()

    def test_missing_hs_code_blocks_without_calling_fdms(self):
        self.product_a.hs_code = False
        inv = self._invoice()
        self.assertTrue(inv.fiscal_issues)
        self._pay(inv)
        api = self._run_queue(return_value=ok_response())
        api.assert_not_called()
        self.assertEqual(inv.fiscal_state, 'blocked')

    def test_strict_confirm_blocks_on_issues(self):
        self.product_a.hs_code = False
        inv = self._invoice(post=False)
        with self.assertRaises(UserError):
            inv.with_context(fiscal_preflight_strict=True).action_post()

    def test_stuck_submission_needs_check(self):
        inv = self._invoice()
        inv.write({'fiscal_state': 'submitting',
                   'fiscal_submitted_at': fields.Datetime.now() - timedelta(hours=1)})
        self._run_queue(return_value=ok_response())
        self.assertEqual(inv.fiscal_state, 'needs_check')

    def test_closed_day_waits_without_attempt(self):
        self.device.fiscal_day_status = 'FISCALDAYCLOSED'
        inv = self._invoice()
        self._pay(inv)
        api = self._run_queue(return_value=ok_response())
        api.assert_not_called()
        self.assertEqual(inv.fiscal_state, 'awaiting_payment')
        self.assertEqual(inv.fiscal_attempts, 0)

    def test_fiscalised_invoice_cannot_be_reset(self):
        inv = self._invoice()
        self._pay(inv)
        self._run_queue(return_value=ok_response())
        with self.assertRaises(UserError):
            inv.button_draft()

    def test_manual_fiscalise_queues_unpaid_invoice(self):
        inv = self._invoice()
        inv.action_fiscalise_invoice()
        self.assertEqual(inv.fiscal_state, 'pending')
        self._run_queue(return_value=ok_response())
        self.assertEqual(inv.fiscal_state, 'done', inv.fiscal_last_error)

    def test_vendor_bill_ignored(self):
        bill = self.init_invoice('in_invoice', partner=self.partner_a, products=self.product_a,
                                 post=True)
        self.assertEqual(bill.fiscal_state, 'not_applicable')

    def test_fiscalised_pdf_replaces_frozen_copy(self):
        inv = self._invoice()
        Send = self.env['account.move.send'].with_context(force_report_rendering=True)
        data = {inv: {'pdf_report': Send._get_default_pdf_report_id(inv)}}
        Send._prepare_invoice_pdf_report(data)
        Send._link_invoice_documents(data)  # simulate the PDF frozen when first sent
        frozen = inv.invoice_pdf_report_id
        self.assertTrue(frozen)
        self._pay(inv)
        with patch(DEVICE_CLS + '._api_request', autospec=True, return_value=ok_response()):
            self.Move.with_context(force_report_rendering=True)._cron_fiscalise_queue()
        inv.invalidate_recordset()
        self.assertEqual(inv.fiscal_state, 'done')
        self.assertTrue(inv.invoice_pdf_report_id)
        self.assertNotEqual(inv.invoice_pdf_report_id, frozen)
        self.assertFalse(frozen.sudo().res_field)  # old copy kept for audit, detached

    def test_day_status_casing_is_normalised(self):
        self.device._process_status_response({'fiscalDayStatus': 'FiscalDayClosed'})
        self.assertEqual(self.device.fiscal_day_status, 'FISCALDAYCLOSED')
        self.assertFalse(self.device.is_day_open)
