from odoo import models, fields

class ProductTemplate(models.Model):
    _inherit = 'product.template'

    hs_code = fields.Char(
        string='HS Code', 
        help='Harmonized System Code for the product'
    )

class ProductProduct(models.Model):
    _inherit = 'product.product'

    hs_code = fields.Char(
        string='HS Code',
        related='product_tmpl_id.hs_code',
        store=True,  # Optional: stores the value in database for faster searches
        readonly=False,  # Allows editing from product variant
        help='Harmonized System Code for the product'
    )