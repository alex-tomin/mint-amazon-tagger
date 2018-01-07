from collections import defaultdict, Counter
from copy import deepcopy
import csv
from datetime import datetime
from pprint import pformat, pprint
import string

from algorithm_u import algorithm_u
import category
from currency import micro_usd_nearly_equal, micro_usd_to_usd_string, parse_usd_as_micro_usd, CENT_MICRO_USD, MICRO_USD_EPS
from mint import truncate_title

PRINTABLE = set(string.printable)


def get_title(amzn_obj, target_length):
    # Also works for a Refund record.
    qty = amzn_obj.quantity
    base_str = None
    if qty > 1:
        base_str = str(qty) + 'x'
    # Remove non-ASCII characters from the title.
    clean_title = ''.join(filter(lambda x: x in PRINTABLE, amzn_obj.title))
    return truncate_title(clean_title, target_length, base_str)


CURRENCY_FIELD_NAMES = set([
    'Item Subtotal',
    'Item Subtotal Tax',
    'Item Total',
    'List Price Per Unit',
    'Purchase Price Per Unit',
    'Refund Amount',
    'Refund Tax Amount',
    'Shipping Charge',
    'Subtotal',
    'Tax Charged',
    'Tax Before Promotions',
    'Total Charged',
    'Total Promotions',
])

DATE_FIELD_NAMES = set([
    'Order Date',
    'Refund Date',
    'Shipment Date',
])

RENAME_FIELD_NAMES = {
    'Carrier Name & Tracking Number': 'tracking',
}


def pythonify_amazon_dict(raw_dict):
    keys = set(raw_dict.keys())

    # Convert to microdollar ints
    for ck in keys & CURRENCY_FIELD_NAMES:
        raw_dict[ck] = parse_usd_as_micro_usd(raw_dict[ck])

    # Convert to datetime.date
    for dk in keys & DATE_FIELD_NAMES:
        raw_dict[dk] = parse_amazon_date(raw_dict[dk])

    # Rename long or unpythonic names:
    for old_key in keys & RENAME_FIELD_NAMES.keys():
        new_key = RENAME_FIELD_NAMES[old_key]
        raw_dict[new_key] = raw_dict[old_key]
        del raw_dict[old_key]

    if 'Quantity' in keys:
        raw_dict['Quantity'] = int(raw_dict['Quantity'])

    return dict([(k.lower().replace(' ', '_').replace('/', '_'), v) for k, v in raw_dict.items()])


def parse_amazon_date(date_str):
    if not date_str:
        return None
    try:
        return datetime.strptime(date_str, '%m/%d/%Y').date()
    except ValueError:
        return datetime.strptime(date_str, '%m/%d/%y').date()


def associate_items_with_orders(orders, items):
    items_by_oid = defaultdict(list)
    for i in items:
        items_by_oid[i.order_id].append(i)
    orders_by_oid = defaultdict(list)
    for o in orders:
        orders_by_oid[o.order_id].append(o)

    for oid, orders in orders_by_oid.items():
        oid_items = items_by_oid[oid]

        subtotal_equal = micro_usd_nearly_equal(
            Order.sum_subtotals(orders),
            Item.sum_subtotals(oid_items))
        assert subtotal_equal
        
        if len(orders) == 1:
            orders[0].set_items(oid_items, assert_unmatched=True)
            continue

        # First try to divy up the items by tracking.
        items_by_tracking = defaultdict(list)
        for i in oid_items:
            items_by_tracking[i.tracking].append(i)

        # It is never the case that multiple orders with the same order id will
        # have the same tracking number.
        for order in orders:
            items = items_by_tracking[order.tracking]
            if micro_usd_nearly_equal(
                    Item.sum_subtotals(items),
                    order.subtotal):
                # A perfect fit.
                order.set_items(items, assert_unmatched=True)
                # Remove the selected items.
                oid_items = [i for i in oid_items if i not in items]
        # Remove orders that have items.
        orders = [o for o in orders if not o.items]
        if not orders and not oid_items:
            continue

        orders = sorted(orders, key=lambda o: o.subtotal)
        
        # Partition the remaining items into every possible arrangement and
        # validate against the remaining orders.
        for item_groupings in algorithm_u(oid_items, len(orders)):
            subtotals_with_groupings = sorted(
                [(Item.sum_subtotals(items), items) for items in item_groupings],
                key=lambda g: g[0])
            if all([micro_usd_nearly_equal(
                    subtotals_with_groupings[i][0],
                    orders[i].subtotal) for i in range(len(orders))]):
                for idx, order in enumerate(orders):
                    order.set_items(subtotals_with_groupings[idx][1], assert_unmatched=True)
                break


ORDER_MERGE_FIELDS = {
    'shipping_charge',
    'subtotal',
    'tax_before_promotions',
    'tax_charged',
    'total_charged',
    'total_promotions',
}


class Order:
    matched = False
    trans_id = None
    items = []
    
    def __init__(self, raw_dict):
        self.__dict__.update(pythonify_amazon_dict(raw_dict))
        
    @classmethod
    def parse_from_csv(cls, csv_file):
        return [cls(raw_dict) for raw_dict in csv.DictReader(csv_file)]

    @staticmethod
    def sum_subtotals(orders):
        return sum([o.subtotal for o in orders])

    def total_by_items(self):
        return Item.sum_totals(self.items) + self.shipping_charge - self.total_promotions

    def total_by_subtotals(self):
        return self.subtotal + self.tax_charged + self.shipping_charge - self.total_promotions

    def transact_date(self):
        return self.shipment_date

    def transact_amount(self):
        return self.total_charged
    
    def match(self, trans):
        self.matched = True
        self.trans_id = trans.id

    def get_invoice_url(self):
        return 'https://www.amazon.com/gp/css/summary/print.html?ie=UTF8&orderID={oid}'.format(oid=self.order_id)
    
    def set_items(self, items, assert_unmatched=False):
        self.items = items
        for i in items:
            if assert_unmatched:
                assert not i.matched
            i.matched = True
            i.order = self

    def get_note(self):
        return (
            'Amazon order id: {}\n'
            'Order date: {}\n'
            'Ship date: {}\n'
            'Tracking: {}').format(
                self.order_id,
                self.order_date,
                self.shipment_date,
                self.tracking)

    def attribute_subtotal_diff_to_misc_charge(self):
        diff = self.total_charged - self.total_by_subtotals()
        if diff < MICRO_USD_EPS:
            return False

        self.subtotal += diff

        adjustment = deepcopy(self.items[0])
        adjustment.title = 'Misc Charge (Gift wrap, etc)'
        adjustment.category = 'Shopping'
        adjustment.quantity = 1
        adjustment.item_total = diff
        adjustment.item_subtotal = diff
        adjustment.item_subtotal_tax = 0

        self.items.append(adjustment)
        return True

    def attribute_itemized_diff_to_shipping_tax(self):
        # Shipping [sometimes] has tax. Include this in the shipping charge.
        # Unfortunately Amazon doesn't provide this anywhere; it must be
        # inferred as of now.
        if not self.shipping_charge:
            return False

        diff = self.total_charged - self.total_by_items()
        if diff < MICRO_USD_EPS:
            return False

        self.shipping_charge += diff

        self.tax_charged -= diff
        self.tax_before_promotions -= diff

        return True

    def attribute_itemized_diff_to_per_item_tax(self):
        itemized_sum = self.total_by_items()
        itemized_diff = self.total_charged - itemized_sum
        if abs(itemized_diff) < MICRO_USD_EPS:
            return False

        itemized_tax = Item.sum_subtotals_tax(self.items)
        tax_diff = self.tax_charged - itemized_tax
        if itemized_diff - tax_diff > MICRO_USD_EPS:
            return False

        # The per-item tax was not computed correctly; the tax miscalculation
        # matches the itemized difference. Sometimes AMZN is bad at math (lol),
        # and most of the time it's a smiply rounding error. To keep the line
        # items adding up correctly, spread the tax difference across the
        # items.
        tax_rate_per_item = [
            round(i.item_subtotal_tax * 100.0 / i.item_subtotal, 1)
            for i in self.items]
        while abs(tax_diff) > MICRO_USD_EPS:
            if abs(tax_diff) < CENT_MICRO_USD:
                # If the difference is under a penny, round that
                # partial cent to the first item.
                adjust_amount = tax_diff
                adjust_idx = 0
            elif tax_diff > 0:
                adjust_idx = None
                min_rate = None
                for (idx, rate) in enumerate(tax_rate_per_item):
                    if rate != 0 and (not min_rate or rate < min_rate):
                        adjust_idx = idx
                        min_rate = rate
                adjust_amount = CENT_MICRO_USD
            else:
                # Find the highest taxed item (by rate) and discount it
                # a penny.
                (adjust_idx, _) = max(
                    enumerate(tax_rate_per_item), key=lambda x: x[1])
                adjust_amount = -CENT_MICRO_USD

            self.items[adjust_idx].item_subtotal_tax += adjust_amount
            self.items[adjust_idx].item_total += adjust_amount
            tax_diff -= adjust_amount
            tax_rate_per_item[adjust_idx] = round(
                self.items[adjust_idx].item_subtotal_tax * 100.0 /
                self.items[adjust_idx].item_subtotal, 1)
        return True

    def to_mint_transactions(self, t):
        new_transactions = []
        
        # More expensive items are always more interesting when it comes to
        # budgeting, so show those first (for both itemized and concatted).
        items = sorted(self.items, key=lambda item: item.item_total, reverse=True)

        # Itemize line-items:
        for i in items:
            item = t.split(
                amount=i.item_total,
                category=category.AMAZON_TO_MINT_CATEGORY.get(
                    i.category, category.DEFAULT_MINT_CATEGORY),
                desc=i.get_title(88),
                note=self.get_note())
            new_transactions.append(item)

        # Itemize the shipping cost, if any.
        ship = None
        if self.shipping_charge:
            ship = t.split(
                amount=self.shipping_charge,
                category='Shipping',
                desc='Shipping',
                note=self.get_note())

            new_transactions.append(ship)

        # All promotion(s) as one line-item.
        promo = None
        if self.total_promotions:
            promo = t.split(
                amount=-self.total_promotions,
                category=category.DEFAULT_MINT_CATEGORY,
                desc='Promotion(s)',
                note=self.get_note(),
                isDebit=False)
            new_transactions.append(promo)

        # If there was a promo that matches the shipping cost, it's nearly
        # certainly a Free One-day/same-day/etc promo. In this case, categorize
        # the promo instead as 'Shipping', which will cancel out in Mint
        # trends.

        if promo and ship and micro_usd_nearly_equal(-promo.amount, ship.amount):
            promo.category = 'Shipping'

        return new_transactions

    @classmethod
    def merge(cls, orders):
        if len(orders) == 1:
            result = orders[0]
            result.set_items(Item.merge(result.items))
            return result
        
        result = deepcopy(orders[0])
        result.set_items(Item.merge([i for o in orders for i in o.items ]))
        for key in ORDER_MERGE_FIELDS:
            result.__dict__[key] = sum([o.__dict__[key] for o in orders])
        return result

    def __repr__(self):
        return 'Order ({id}): {date} Total {total}\tSubtotal {subtotal}\tTax {tax}\tPromo {promo}\tShip {ship}\tItems: \n{items}'.format(
            id=self.order_id, date=(self.shipment_date or self.order_date), total=micro_usd_to_usd_string(self.total_charged), subtotal=micro_usd_to_usd_string(self.subtotal), tax=micro_usd_to_usd_string(self.tax_charged), promo=micro_usd_to_usd_string(self.total_promotions), ship=micro_usd_to_usd_string(self.shipping_charge), items=pformat(self.items))


class Item:
    matched = False
    order = None

    def __init__(self, raw_dict):
        self.__dict__.update(pythonify_amazon_dict(raw_dict))
        self.__dict__['original_item_subtotal_tax'] = self.item_subtotal_tax

    @classmethod
    def parse_from_csv(cls, csv_file):
        return [cls(raw_dict) for raw_dict in csv.DictReader(csv_file)]
    
    @staticmethod
    def sum_subtotals(items):
        return sum([i.item_subtotal for i in items])

    @staticmethod
    def sum_totals(items):
        return sum([i.item_total for i in items])

    @staticmethod
    def sum_subtotals_tax(items):
        return sum([i.item_subtotal_tax for i in items])

    def get_title(self, target_length=100):
        return get_title(self, target_length)

    def is_cancelled(self):
        return self.order_status == 'Cancelled'

    def get_note(self):
        return (
            'Amazon order id: {}\n'
            'Order date: {}\n'
            'Ship date: {}\n'
            'Tracking: {}').format(
                self.order_id,
                self.order_date,
                self.shipment_date,
                self.tracking)

    def set_quantity(self, new_quantity):
        """Sets the quantity of this item and updates all prices."""
        original_quantity = self.quantity

        assert new_quantity > 0
        subtotal_equal = micro_usd_nearly_equal(
            self.purchase_price_per_unit * original_quantity,
            self.item_subtotal)
        assert subtotal_equal < MICRO_USD_EPS

        self.item_subtotal = self.purchase_price_per_unit * new_quantity
        self.item_subtotal_tax = (
            self.item_subtotal_tax / original_quantity) * new_quantity
        self.item_total = self.item_subtotal + self.item_subtotal_tax
        self.quantity = new_quantity

    def split_by_quantity(self):
        """Splits this item into 'quantity' items."""
        if self.quantity == 1:
            return [self]
        orig_qty = self.quantity
        self.set_quantity(1)
        return [deepcopy(self) for i in range(orig_qty)]

    @classmethod
    def merge(cls, items):
        """Collapses identical items by using quantity."""
        if len(items) < 2:
            return items
        unique_items = defaultdict(list)
        for i in items:
            key = '{}-{}-{}'.format(
                i.title,
                i.asin_isbn,
                i.item_subtotal)
            unique_items[key].append(i)
        results = []
        for same_items in unique_items.values():
            qty = len(same_items)
            if qty == 1:
                results.extend(same_items)
                continue

            item = deepcopy(same_items[0])
            item.set_quantity(qty)
            results.append(item)
        return results
    
    def __repr__(self):
        return '{qty} of Item: Total {total}\tSubtotal {subtotal}\tTax {tax} {desc}'.format(
            qty=self.quantity, total=micro_usd_to_usd_string(self.item_total), subtotal=micro_usd_to_usd_string(self.item_subtotal), tax=micro_usd_to_usd_string(self.item_subtotal_tax), desc=self.title)


class Refund:
    matched = False
    trans_id = None

    def __init__(self, raw_dict):
        # Refunds are rad: AMZN doesn't total the tax + sub-total for you.
        fields = pythonify_amazon_dict(raw_dict)
        fields['total_refund_amount'] = (
            fields['refund_amount'] + fields['refund_tax_amount'])
        self.__dict__.update(fields)

    @staticmethod
    def sum_total_refunds(refunds):
        return sum([r.total_refund_amount for r in refunds])

    @classmethod
    def parse_from_csv(cls, csv_file):
        return [cls(raw_dict) for raw_dict in csv.DictReader(csv_file)]

    def match(self, trans):
        self.matched = True
        self.trans_id = trans.id

    def transact_date(self):
        return self.refund_date

    def transact_amount(self):
        return -self.total_refund_amount
    
    def get_title(self, target_length=100):
        return get_title(self, target_length)

    def get_note(self):
        return (
            'Amazon refund for order id: {}\n'
            'Order date: {}\n'
            'Refund date: {}\n'
            'Refund reason: {}').format(
                self.order_id,
                self.order_date,
                self.refund_date,
                self.refund_reason)

    def to_mint_transaction(self, t):
        result = t.split(
            desc=self.get_title(88),
            category=category.AMAZON_TO_MINT_CATEGORY.get(
                self.category, category.DEFAULT_MINT_RETURN_CATEGORY),
            amount=-self.total_refund_amount,
            note = self.get_note(),
            isDebit=False)
        return result


    @staticmethod
    def merge(refunds):
        """Collapses identical items by using quantity."""
        if len(refunds) <= 1:
            return refunds
        unique_refund_items = defaultdict(list)
        for r in refunds:
            key = '{}-{}-{}-{}-{}'.format(
                r.refund_date,
                r.refund_reason,
                r.title,
                r.total_refund_amount,
                r.asin_isbn)
            unique_refund_items[key].append(r)
        results = []
        for same_items in unique_refund_items.values():
            qty = len(same_items)
            if qty == 1:
                results.extend(same_items)
                continue

            refund = same_items[0]
            refund.quantity = qty
            refund.total_refund_amount *= qty
            refund.refund_amount *= qty
            refund.refund_tax_amount *= qty
            
            results.append(refund)
        return results

    def __repr__(self):
        return '{qty} of Refund: Total {total}\tSubtotal {subtotal}\tTax {tax} {desc}'.format(
            qty=self.quantity, total=micro_usd_to_usd_string(self.total_refund_amount), subtotal=micro_usd_to_usd_string(self.refund_amount), tax=micro_usd_to_usd_string(self.refund_tax_amount), desc=self.title)