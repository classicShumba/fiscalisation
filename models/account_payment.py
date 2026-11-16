from odoo import models


class AccountPayment(models.Model):
    _inherit = 'account.payment'

    def action_post(self):
        res = super().action_post()
        # Payments without journal entries mark invoices 'in_payment' via matched_payment_ids
        # without a reconciliation; nudge the fiscalisation queue so they are picked up promptly.
        if self.filtered(lambda p: p.payment_type == 'inbound'):
            self.env['account.move']._fiscal_trigger_cron()
        return res
