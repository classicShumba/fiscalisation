from odoo import api, models


class AccountPartialReconcile(models.Model):
    _inherit = 'account.partial.reconcile'

    @api.model_create_multi
    def create(self, vals_list):
        partials = super().create(vals_list)
        # A payment (manual, gateway capture, bank reconciliation, credit) was tied to an invoice.
        moves = (partials.debit_move_id | partials.credit_move_id).move_id
        if moves.filtered(lambda m: m.move_type == 'out_invoice' and m.fiscal_state == 'awaiting_payment'):
            self.env['account.move']._fiscal_trigger_cron()
        # A credit note applied to an already fiscalised invoice can now be sent.
        moves._fiscal_release_credit_notes()
        return partials
