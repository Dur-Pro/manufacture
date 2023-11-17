# Copyright 2021 ForgeFlow S.L. (https://www.forgeflow.com)
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl.html).

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError
from odoo.tools.float_utils import float_compare, float_is_zero


class MrpProductionSerialMatrix(models.TransientModel):
    _name = "mrp.production.serial.matrix"
    _description = "Mrp Production Serial Matrix"

    production_id = fields.Many2one(
        comodel_name="mrp.production",
        string="Manufacturing Order",
        readonly=True,
    )
    product_id = fields.Many2one(
        related="production_id.product_id",
        readonly=True,
    )
    company_id = fields.Many2one(
        related="production_id.company_id",
        readonly=True,
    )
    finished_lot_ids = fields.Many2many(
        string="Finished Product Serial Numbers",
        comodel_name="stock.lot",
        domain="[('product_id', '=', product_id)]",
    )
    line_ids = fields.One2many(
        string="Matrix Cell",
        comodel_name="mrp.production.serial.matrix.line",
        inverse_name="wizard_id",
    )
    lot_selection_warning_msg = fields.Char(compute="_compute_lot_selection_warning")
    lot_selection_warning_ids = fields.Many2many(
        comodel_name="stock.lot", compute="_compute_lot_selection_warning"
    )
    lot_selection_warning_count = fields.Integer(
        compute="_compute_lot_selection_warning"
    )
    include_lots = fields.Boolean(
        string="Include Lots?",
        default=True,
        help="Include products tracked by Lots in matrix. Product tracket by "
             "serial numbers are always included.",
    )

    @api.depends("line_ids", "line_ids.component_lot_id")
    def _compute_lot_selection_warning(self):
        for rec in self:
            warning_lots = self.env["stock.lot"]
            warning_msgs = []
            # Serials:
            serial_lines = rec.line_ids.filtered(
                lambda l: l.component_id.tracking == "serial"
            )
            serial_counter = {}
            for sl in serial_lines:
                if not sl.component_lot_id:
                    continue
                serial_counter.setdefault(sl.component_lot_id, 0)
                serial_counter[sl.component_lot_id] += 1
            for lot, counter in serial_counter.items():
                if counter > 1:
                    warning_lots += lot
                    warning_msgs.append(
                        "Serial number %s selected several times" % lot.name
                    )
            # Lots
            lot_lines = rec.line_ids.filtered(
                lambda l: l.component_id.tracking == "lot"
            )
            lot_consumption = {}
            for ll in lot_lines:
                if not ll.component_lot_id:
                    continue
                lot_consumption.setdefault(ll.component_lot_id, 0)
                free_qty, reserved_qty = ll._get_available_and_reserved_quantities()
                available_quantity = free_qty + reserved_qty
                if (
                    available_quantity - lot_consumption[ll.component_lot_id]
                    < ll.lot_qty
                ):
                    warning_lots += ll.component_lot_id
                    warning_msgs.append(
                        "Lot %s not available at the needed qty (%s/%s)"
                        % (ll.component_lot_id.name, available_quantity, ll.lot_qty)
                    )
                lot_consumption[ll.component_lot_id] += ll.lot_qty

            not_filled_lines = rec.line_ids.filtered(
                lambda l: l.finished_lot_id and not l.component_lot_id
            )
            if not_filled_lines:
                not_filled_finshed_lots = not_filled_lines.mapped("finished_lot_id")
                warning_lots += not_filled_finshed_lots
                warning_msgs.append(
                    "Some cells are not filled for some finished serial number (%s)"
                    % ", ".join(not_filled_finshed_lots.mapped("name"))
                )
            rec.lot_selection_warning_msg = ", ".join(warning_msgs)
            rec.lot_selection_warning_ids = warning_lots
            rec.lot_selection_warning_count = len(warning_lots)

    @api.model
    def default_get(self, fields):
        res = super().default_get(fields)
        production_id = self.env.context["active_id"]
        active_model = self.env.context["active_model"]
        if not production_id:
            return res
        assert active_model == "mrp.production", "Bad context propagation"
        production = self.env["mrp.production"].browse(production_id)
        if not production.show_serial_matrix:
            raise UserError(
                _("The finished product of this MO is not tracked by serial numbers.")
            )

        finished_lots = self.env["stock.lot"]
        if production.lot_producing_id:
            finished_lots = production.lot_producing_id

        matrix_lines = self._get_matrix_lines(production, finished_lots)

        res.update(
            {
                "line_ids": [(0, 0, x) for x in matrix_lines],
                "production_id": production_id,
                "finished_lot_ids": [(4, lot_id, 0) for lot_id in finished_lots.ids],
            }
        )
        return res

    def _get_matrix_lines(self, production, finished_lots):
        tracked_components = []
        for move in production.move_raw_ids:
            rounding = move.product_id.uom_id.rounding
            if float_is_zero(move.product_qty, precision_rounding=rounding):
                # Component moves cannot be deleted in in-progress MO's; however,
                # they can be set to 0 units to consume. In such case, we ignore
                # the move.
                continue
            boml = move.bom_line_id
            # TODO: UoM (MO/BoM using different UoMs than product's defaults).
            if boml:
                qty_per_finished_unit = boml.product_qty / boml.bom_id.product_qty
            else:
                # The product could have been added for the specific MO but not
                # be part of the BoM.
                qty_per_finished_unit = move.product_qty / production.product_qty
            if move.product_id.tracking == "serial":
                for i in range(1, int(qty_per_finished_unit) + 1):
                    tracked_components.append((move.product_id, i, 1))
            elif move.product_id.tracking == "lot" and self.include_lots:
                tracked_components.append((move.product_id, 0, qty_per_finished_unit))

        matrix_lines = []
        current_lot = False
        new_lot_number = 0
        for _i in range(int(production.product_qty)):
            if finished_lots:
                current_lot = finished_lots[0]
            else:
                new_lot_number += 1
            for component_tuple in tracked_components:
                line = self._prepare_matrix_line(
                    component_tuple, finished_lot=current_lot, number=new_lot_number
                )
                matrix_lines.append(line)
            if current_lot:
                finished_lots -= current_lot
                current_lot = False
        return matrix_lines

    def _prepare_matrix_line(self, component_tuple, finished_lot=None, number=None):
        component, lot_no, lot_qty = component_tuple
        column_name = component.display_name
        if lot_no > 0:
            column_name += " (%s)" % lot_no
        res = {
            "component_id": component.id,
            "component_column_name": column_name,
            "lot_qty": lot_qty,
        }
        if finished_lot:
            if isinstance(finished_lot.id, models.NewId):
                # NewId instances are not handled correctly later, this is a
                # small workaround. In future versions it might not be needed.
                lot_id = finished_lot.id.origin
            else:
                lot_id = finished_lot.id
            res.update(
                {
                    "finished_lot_id": lot_id,
                    "finished_lot_name": finished_lot.name,
                }
            )
        elif isinstance(number, int):
            res.update(
                {
                    "finished_lot_name": _("(New Lot %s)") % number,
                }
            )
        return res

    @api.onchange("finished_lot_ids", "include_lots")
    def _onchange_finished_lot_ids(self):
        for rec in self:
            matrix_lines = self._get_matrix_lines(
                rec.production_id,
                rec.finished_lot_ids,
            )
            rec.line_ids = False
            rec.write({"line_ids": [(0, 0, x) for x in matrix_lines]})

    def button_validate(self):
        self.ensure_one()
        if self.lot_selection_warning_count > 0:
            raise UserError(
                _("Some issues has been detected in your selection: %s")
                % self.lot_selection_warning_msg
            )
        current_mo = self.production_id
        if current_mo.product_qty > 1:
            mos = current_mo._split_productions({current_mo: [1 for i in self.finished_lot_ids]})
        else:
            mos = [current_mo]

        for index, finished_lot in enumerate(self.finished_lot_ids):
            mo = mos[index]
            mo.write({"lot_producing_id": finished_lot.id,
                      "qty_producing": 1.0})
            mo.action_confirm()
            for move in mo.move_raw_ids + mo.move_byproduct_ids:
                matrix_line = self.line_ids.filtered(lambda l: (l.finished_lot_id == mo.lot_producing_id
                                                                or l.finished_lot_name == mo.lot_producing_id.name) and
                                                               l.component_id == move.product_id)
                # If there is no matrix line, then the lot/serial tracking is not active for this component
                if matrix_line:
                    move._update_reserved_quantity(move.product_uom_qty, matrix_line.lot_qty, move.location_id,
                                                   lot_id=matrix_line.component_lot_id)
                else:
                    move._update_reserved_quantity(move.product_uom_qty, move.product_uom_qty, move.location_id)
                for line in move.move_line_ids:
                    line.write({"qty_done": line.product_qty})
        for mo in mos:
            mo.button_mark_done()

        res = {
            "domain": [("id", "in", mos.ids)],
            "name": _("Manufacturing Orders"),
            "src_model": "mrp.production.serial.matrix",
            "view_type": "form",
            "view_mode": "tree,form",
            "view_id": False,
            "views": False,
            "res_model": "mrp.production",
            "type": "ir.actions.act_window",
        }
        return res
