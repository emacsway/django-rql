from __future__ import unicode_literals


from __future__ import unicode_literals

from django.db.models import Q
from lark import Transformer, Tree

from dj_rql.constants import ComparisonOperators, LogicalOperators


class RQLToDjangoORMTransformer(Transformer):
    """ Parsed RQL AST tree transformer to Django ORM Query.

    Notes:
        Grammar-Function name mapping is made automatically by Lark.
    """
    def __init__(self, filter_cls_instance):
        self._filter_cls_instance = filter_cls_instance

    def start(self, args):
        return self._filter_cls_instance.queryset.filter(args[0]).distinct()

    def comp(self, args):
        prop_index = 1
        value_index = 2

        if len(args) == 2:
            # id=1
            operation = ComparisonOperators.EQ
            prop_index = 0
            value_index = 1
        elif args[0].data == 'comp_term':
            # eq(id,1)
            operation = self._get_value(args[0])
        else:
            # id=eq=1
            operation = self._get_value(args[1])
            prop_index = 0

        return self._filter_cls_instance.get_django_q_for_filter_expression(
            self._get_value(args[prop_index]), operation, self._get_value(args[value_index])
        )

    def logical(self, args):
        operation = args[0].data
        children = args[0].children
        if operation == LogicalOperators.get_grammar_key(LogicalOperators.NOT):
            return ~Q(children[0])
        if operation == LogicalOperators.get_grammar_key(LogicalOperators.AND):
            return Q(*children)

        q = Q()
        for child in children:
            q |= child
        return q

    def term(self, args):
        return args[0]

    def expr_term(self, args):
        return args[0]

    @staticmethod
    def _get_value(obj):
        while isinstance(obj, Tree):
            obj = obj.children[0]
        return obj.value