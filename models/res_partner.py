from odoo import models, fields

class ResPartner(models.Model):
    _inherit = 'res.partner'

    tin = fields.Char(string='TIN Number')
