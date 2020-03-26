from uuid import uuid4

from django.db.models import Q
from django.utils.dateparse import parse_date, parse_datetime
from lark.exceptions import LarkError

from dj_rql.constants import (
    ComparisonOperators,
    DjangoLookups,
    FilterLookups,
    FilterTypes,
    ListOperators,
    SearchOperators,
    RESERVED_FILTER_NAMES,
    RQL_ANY_SYMBOL,
    RQL_EMPTY,
    RQL_FALSE,
    RQL_NULL,
    RQL_SEARCH_PARAM,
    RQL_TRUE,
    SUPPORTED_FIELD_TYPES,
)
from dj_rql.exceptions import RQLFilterLookupError, RQLFilterValueError, RQLFilterParsingError
from dj_rql.parser import RQLParser
from dj_rql.qs import Annotation
from dj_rql.transformer import RQLToDjangoORMTransformer

iterable_types = (list, tuple)


class RQLFilterClass(object):
    MODEL = None
    FILTERS = None
    EXTENDED_SEARCH_ORM_ROUTES = tuple()
    DISTINCT = False
    SELECT = False

    def __init__(self, queryset):
        assert self.MODEL, 'Model must be set for Filter Class.'
        assert isinstance(self.FILTERS, iterable_types) and self.FILTERS, \
            'List of filters must be set for Filter Class.'
        assert isinstance(self.EXTENDED_SEARCH_ORM_ROUTES, iterable_types), \
            'Extended search ORM routes must be iterable.'

        self._is_distinct = self.DISTINCT

        self.ordering_filters = set()
        self.search_filters = set()

        self.filters = {}
        self._select_tree = {}
        self._default_exclusions = set()

        self._build_filters(self.FILTERS)

        self.queryset = queryset

    def build_q_for_custom_filter(self, filter_name, operator, str_value, **kwargs):
        """ Django Q() builder for custom filter.

        Args:
            filter_name (str): Name of the filter.
            operator (str): RQL grammar operator, like `eq`.
            str_value (str): String filter value.
        """
        raise RQLFilterParsingError(details={
            'error': 'Filter logic is not implemented: {}.'.format(filter_name),
        })

    def build_name_for_custom_ordering(self, filter_name):
        """ Builder for ordering name of custom filter.

        Args:
            filter_name (str): Name of the filter.
        """
        raise RQLFilterParsingError(details={
            'error': 'Ordering logic is not implemented: {}.'.format(filter_name),
        })

    def apply_filters(self, query, request=None):
        """ Entry point function for model queryset filtering. """
        self._is_distinct = self.DISTINCT
        rql_ast, qs, select = None, self.queryset, []

        if query:
            rql_ast = RQLParser.parse_query(query)
            rql_transformer = RQLToDjangoORMTransformer(self)

            try:
                qs = rql_transformer.transform(rql_ast)
            except LarkError as e:
                # Lark reraises it's errors, but the original ones are needed
                raise e.orig_exc

            qs = self._apply_ordering(qs, rql_transformer.ordering_filters)
            select = rql_transformer.select_filters

            if self._is_distinct:
                qs = qs.distinct()

        if request:
            setattr(request, 'rql_ast', rql_ast)

        if self.SELECT:
            rql_select = self._build_rql_select(select)
            qs = self._apply_optimizations(qs, rql_select)

            if request:
                setattr(request, 'rql_select', {
                    'depth': 0,
                    'select': rql_select,
                })

        self.queryset = qs

        return rql_ast, qs

    def build_q_for_filter(self, filter_name, operator, str_value, list_operator=None):
        """ Django Q() builder for the given expression. """
        if filter_name == RQL_SEARCH_PARAM:
            return self._build_q_for_search(operator, str_value)

        base_item = self.get_filter_base_item(filter_name)
        if not base_item:
            return Q()

        if base_item.get('distinct'):
            self._is_distinct = True

        filter_item = self.filters[filter_name]
        available_lookups = base_item.get('lookups', set())
        if list_operator:
            list_filter_lookup = FilterLookups.IN \
                if list_operator == ListOperators.IN \
                else FilterLookups.OUT
            if list_filter_lookup not in available_lookups:
                raise RQLFilterLookupError(**self._get_error_details(
                    filter_name, list_filter_lookup, str_value,
                ))

        null_values = base_item.get('null_values', set())
        filter_lookup = self._get_filter_lookup(
            filter_name, operator, str_value, available_lookups, null_values,
        )
        django_lookup = self._get_django_lookup(filter_lookup, str_value, null_values)

        if base_item.get('custom'):
            return self.build_q_for_custom_filter(
                filter_name,
                operator,
                str_value,
                list_operator=list_operator,
                filter_lookup=filter_lookup,
                django_lookup=django_lookup,
            )

        django_field = base_item['field']
        use_repr = base_item.get('use_repr', False)

        typed_value = self._get_typed_value(
            filter_name, filter_lookup, str_value, django_field,
            use_repr, null_values, django_lookup,
        )

        if not isinstance(filter_item, iterable_types):
            return self._build_django_q(filter_item, django_lookup, filter_lookup, typed_value)

        # filter has different DB field 'sources'
        q = Q()
        for item in filter_item:
            item_q = self._build_django_q(item, django_lookup, filter_lookup, typed_value)
            if filter_lookup == FilterLookups.NE:
                q &= item_q
            else:
                q |= item_q
        return q

    def get_filter_base_item(self, filter_name):
        filter_item = self.filters.get(filter_name)
        if filter_item:
            return filter_item[0] if isinstance(filter_item, iterable_types) else filter_item

    def optimize_field(self, qs, rql_select, field):
        return qs, False

    def _build_rql_select(self, select):
        rql_select = {}

        exclusions, inclusions = set(), set()
        forbidden_inclusions, potential_exclusions = set(), set()

        for select_prop in select:
            # TODO: Check for border +{EMPTY} case
            is_included = (not select_prop[0] == '-')
            filter_name = select_prop[1:] if select_prop[0] in ('-', '+') else select_prop

            select_tree = self._select_tree
            parent_parts = ''
            filter_name_parts = filter_name.split('.')
            last_filter_name_part_index = len(filter_name_parts) - 1

            if is_included:
                for index, part in enumerate(filter_name_parts):
                    if part not in select_tree:
                        # TODO: Check if there can be no such SELECTED field
                        raise

                    if part in exclusions:
                        # TODO: Check if NOT misconfiguration
                        raise

                    if parent_parts + '.' in forbidden_inclusions:
                        # TODO: Check if NOT misconfiguration
                        raise

                    current_part = parent_parts + part
                    inclusions.add(current_part)
                    rql_select[current_part] = True

                    if index != last_filter_name_part_index:
                        parent_parts = current_part + '.'
                        select_tree = select_tree[part]['fields']
                    else:
                        for neighbour_part in select_tree.keys():
                            if neighbour_part != part:
                                potential_exclusions.add(parent_parts + neighbour_part)

            else:
                if filter_name in inclusions:
                    # TODO: Check if NOT misconfiguration
                    raise

                exclusions.add(filter_name)
                rql_select[filter_name] = False

                for index, part in enumerate(filter_name_parts):
                    if part not in select_tree:
                        # TODO: Check if there can be no such SELECTED field
                        raise

                    current_part = parent_parts + part
                    parent_parts = current_part + '.'
                    if index != last_filter_name_part_index:
                        select_tree = select_tree[part]['fields']
                    else:
                        forbidden_inclusions.add(parent_parts)

        for exclusion in potential_exclusions.union(self._default_exclusions) - inclusions:
            rql_select[exclusion] = False

        return rql_select

    def _build_q_for_search(self, operator, str_value):
        if operator != ComparisonOperators.EQ:
            raise RQLFilterParsingError(details={
                'error': 'Bad search filter: {}.'.format(operator),
            })

        unquoted_value = self.remove_quotes(str_value)
        if not unquoted_value.startswith(RQL_ANY_SYMBOL):
            unquoted_value = '*' + unquoted_value

        if not unquoted_value.endswith(RQL_ANY_SYMBOL):
            unquoted_value += '*'

        q = self._build_q_for_extended_search(unquoted_value)
        for filter_name in self.search_filters:
            q |= self.build_q_for_filter(
                filter_name, SearchOperators.I_LIKE, unquoted_value,
            )

        return q

    def _build_q_for_extended_search(self, str_value):
        q = Q()
        extended_search_filter_lookup = FilterLookups.I_LIKE

        for django_orm_route in self.EXTENDED_SEARCH_ORM_ROUTES:
            django_lookup = self._get_searching_django_lookup(
                extended_search_filter_lookup, str_value,
            )
            typed_value = self._get_searching_typed_value(django_lookup, str_value)
            q |= self._build_django_q(
                {'orm_route': django_orm_route},
                django_lookup,
                extended_search_filter_lookup,
                typed_value,
            )

        return q

    def _apply_optimizations(self, qs, rql_select):
        return self.__apply_optimizations(qs, rql_select, self._select_tree)

    def __apply_optimizations(self, qs, rql_select, tree):
        if tree:
            for field_name, field in tree.items():
                if rql_select.get(field['path'], True):
                    qs, optimized = self.optimize_field(qs, rql_select, field)

                    optimization = field['qs']
                    if not optimized and optimization:
                        qs = optimization.apply(qs)
                        qs = self.__apply_optimizations(qs, rql_select, field['fields'])

        return qs

    def _apply_ordering(self, qs, properties):
        if len(properties) == 0:
            return qs
        elif len(properties) > 1:
            raise RQLFilterParsingError(details={
                'error': 'Bad ordering filter: query can contain only one ordering operation.',
            })

        ordering_fields = []
        for prop in properties[0]:
            if '-' == prop[0]:
                filter_name = prop[1:]
                sign = '-'
            else:
                filter_name = prop
                sign = ''
            if filter_name not in self.ordering_filters:
                raise RQLFilterParsingError(details={
                    'error': 'Bad ordering filter: {}.'.format(filter_name),
                })

            filters = self.filters[filter_name]
            if not isinstance(filters, list):
                filters = [filters]
            for f in filters:
                if f.get('distinct'):
                    self._is_distinct = True

                if f.get('custom'):
                    ordering_name = self.build_name_for_custom_ordering(filter_name)
                else:
                    ordering_name = f['orm_route']
                ordering_fields.append('{}{}'.format(sign, ordering_name))

        return qs.order_by(*ordering_fields)

    def _build_filters(self, filters, filter_route='', orm_route='',
                       orm_model=None, select_tree=None, parent_qs=None):
        """ Converter of provided nested filter configuration to linear inner representation. """
        model = orm_model or self.MODEL

        if not orm_route:
            self.filters = {}
            select_tree = self._select_tree

        for item in filters:
            if isinstance(item, str):
                field_filter_route = '{}{}'.format(filter_route, item)
                field_orm_route = '{}{}'.format(orm_route, item)
                field = self._get_field(model, item)
                self._add_filter_item(
                    field_filter_route, self._build_mapped_item(field, field_orm_route),
                )
                self._fill_select_tree(item, field_filter_route, select_tree, parent_qs=parent_qs)
                continue

            if 'namespace' in item:
                for option in ('filter', 'dynamic', 'custom'):
                    assert option not in item, \
                        "{}: '{}' is not supported by namespaces.".format(item['namespace'], option)

                namespace = item['namespace']
                related_filter_route = '{}{}'.format(filter_route, namespace)
                orm_field_name = item.get('source', namespace)
                related_orm_route = '{}{}__'.format(orm_route, orm_field_name)

                related_model = self._get_field(
                    model, orm_field_name, get_related=True,
                ).related_model

                qs = item.get('qs')
                tree = self._fill_select_tree(
                    namespace, related_filter_route, select_tree,
                    namespace=True,
                    hidden=item.get('hidden', False),
                    qs=qs,
                    parent_qs=parent_qs,
                )

                parent_qs = qs if qs else parent_qs
                self._build_filters(
                    item.get('filters', []), related_filter_route + '.',
                    related_orm_route, related_model, select_tree=tree, parent_qs=parent_qs,
                )
                continue

            assert 'filter' in item, "All extended filters must have set 'filter' set."
            filter_name = item['filter']
            field_filter_route = '{}{}'.format(filter_route, filter_name)

            self._fill_select_tree(
                filter_name, field_filter_route, select_tree,
                hidden=item.get('hidden', False),
                qs=item.get('qs'),
                parent_qs=parent_qs,
            )

            if item.get('custom', False):
                self._add_filter_item(field_filter_route, item)
                self._register_ordering_and_search(item, field_filter_route)
                continue

            self._check_use_repr(item, field_filter_route)
            self._check_dynamic(item, field_filter_route, filter_route)

            field = item.get('field')
            kwargs = {
                'lookups': item.get('lookups'),
                'use_repr': item.get('use_repr'),
                'null_values': item.get('null_values'),
                'distinct': item.get('distinct'),
            }

            if 'sources' in item:
                items = []
                for source in item['sources']:
                    full_orm_route = '{}{}'.format(orm_route, source)
                    field = field or self._get_field(model, source)
                    items.append(self._build_mapped_item(field, full_orm_route, **kwargs))
                    self._check_search(item, field_filter_route, field)

            else:
                orm_field_name = item.get('source', filter_name)
                full_orm_route = '{}{}'.format(orm_route, orm_field_name)
                field = field or self._get_field(model, orm_field_name)
                items = self._build_mapped_item(field, full_orm_route, **kwargs)
                self._check_search(item, field_filter_route, field)

            self._add_filter_item(field_filter_route, items)
            self._register_ordering_and_search(item, field_filter_route)

    def _fill_select_tree(self, f_name, full_f_name, select_tree,
                          namespace=False, hidden=False, qs=None, parent_qs=None):
        if not self.SELECT:
            return select_tree

        if hidden:
            self._default_exclusions.add(full_f_name)

        current_select_tree = select_tree
        filter_name_parts = f_name.split('.')
        last_filter_name_part_index = len(filter_name_parts) - 1

        changed_qs = qs
        if qs and parent_qs:
            if isinstance(qs, Annotation):
                changed_qs = qs.__class__(parent=parent_qs, **qs.extensions)
            else:
                changed_qs = qs.__class__(*qs.relations, parent=parent_qs, **qs.extensions)

        for index, filter_name_part in enumerate(filter_name_parts):
            current_select_tree.setdefault(filter_name_part, {
                'hidden': hidden,
                'fields': {},
                'namespace': namespace or (index != last_filter_name_part_index),
                'qs': changed_qs,
                'path': full_f_name,
            })
            current_select_tree = current_select_tree[filter_name_part]['fields']

        return current_select_tree

    def _add_filter_item(self, filter_name, item):
        assert filter_name not in RESERVED_FILTER_NAMES, \
            "'{}' is a reserved filter name.".format(filter_name)
        self.filters[filter_name] = item

    def _register_ordering_and_search(self, item, field_filter_route):
        if item.get('ordering'):
            self.ordering_filters.add(field_filter_route)

        if item.get('search'):
            self.search_filters.add(field_filter_route)

    @classmethod
    def _get_field(cls, base_model, field_name, get_related=False):
        """ Django ORM field getter.

        Notes:
            field_name can have dots or double underscores in them. They are interpreted as
            links to the related models.
        """
        field_name_parts = cls._get_field_name_parts(field_name)
        field_name_parts_length = len(field_name_parts)
        current_model = base_model
        for index, part in enumerate(field_name_parts, start=1):
            current_field = cls._get_model_field(current_model, part)
            if index == field_name_parts_length:
                assert get_related or isinstance(current_field, SUPPORTED_FIELD_TYPES), \
                    'Unsupported field type: {}.'.format(field_name)
                return current_field
            current_model = current_field.related_model

    @staticmethod
    def _get_field_name_parts(field_name):
        return field_name.split('.' if '.' in field_name else '__') if field_name else []

    @classmethod
    def _build_mapped_item(cls,
                           field,
                           field_orm_route,
                           lookups=None,
                           use_repr=None,
                           null_values=None,
                           distinct=None,
                           ):
        possible_lookups = lookups or FilterTypes.default_field_filter_lookups(field)
        if not (field.null or cls._is_pk_field(field)):
            possible_lookups.discard(FilterLookups.NULL)

        result = {
            'field': field,
            'orm_route': field_orm_route,
            'lookups': possible_lookups,
            'null_values': null_values or {RQL_NULL},
            'distinct': distinct or False,
        }

        if use_repr is not None:
            result['use_repr'] = use_repr

        return result

    @staticmethod
    def _is_pk_field(field):
        return field == field.model._meta.pk if hasattr(field, 'model') else False

    @staticmethod
    def _get_model_field(model, field_name):
        return model._meta.get_field(field_name)

    @classmethod
    def _get_filter_lookup(cls, filter_name, operator, str_value, available_lookups, null_values):
        filter_lookup = cls._get_filter_lookup_by_operator(operator)

        if str_value in null_values:
            null_lookups = {FilterLookups.EQ, FilterLookups.NE}
            if (FilterLookups.NULL not in available_lookups) or (filter_lookup not in null_lookups):
                raise RQLFilterLookupError(**cls._get_error_details(
                    filter_name, filter_lookup, str_value,
                ))

        if str_value == RQL_EMPTY:
            available_lookups = {FilterLookups.EQ, FilterLookups.NE}

        if filter_lookup not in available_lookups:
            raise RQLFilterLookupError(**cls._get_error_details(
                filter_name, filter_lookup, str_value,
            ))

        return filter_lookup

    @classmethod
    def _get_django_lookup(cls, filter_lookup, str_value, null_values):
        if str_value in null_values:
            return DjangoLookups.NULL

        if cls._is_searching_lookup(filter_lookup):
            return cls._get_searching_django_lookup(filter_lookup, str_value)

        mapper = {
            FilterLookups.EQ: DjangoLookups.EXACT,
            FilterLookups.NE: DjangoLookups.EXACT,
            FilterLookups.LT: DjangoLookups.LT,
            FilterLookups.LE: DjangoLookups.LTE,
            FilterLookups.GT: DjangoLookups.GT,
            FilterLookups.GE: DjangoLookups.GTE,
        }
        return mapper[filter_lookup]

    @classmethod
    def _get_searching_django_lookup(cls, filter_lookup, str_value):
        val, _ = cls._reflect_like_value(str_value)

        prefix = 'I_' if filter_lookup == FilterLookups.I_LIKE else ''

        pattern = 'REGEX'
        if RQL_ANY_SYMBOL not in val:
            pattern = 'EXACT'
        elif val == RQL_ANY_SYMBOL:
            pattern = 'REGEX'
        else:
            sep_count = val.count(RQL_ANY_SYMBOL)
            if sep_count == 1:
                if val[0] == RQL_ANY_SYMBOL:
                    pattern = 'ENDSWITH'
                elif val[-1] == RQL_ANY_SYMBOL:
                    pattern = 'STARTSWITH'
            elif sep_count == 2 and val[0] == RQL_ANY_SYMBOL == val[-1]:
                pattern = 'CONTAINS'

        return getattr(DjangoLookups, '{}{}'.format(prefix, pattern))

    @classmethod
    def _get_typed_value(cls, filter_name, filter_lookup, str_value, django_field,
                         use_repr, null_values, django_lookup):
        if str_value in null_values:
            return True

        try:
            if cls._is_searching_lookup(filter_lookup):
                return cls._get_searching_typed_value(django_lookup, str_value)

            typed_value = cls._convert_value(django_field, str_value, use_repr=use_repr)
            return typed_value
        except (ValueError, TypeError):
            raise RQLFilterValueError(**cls._get_error_details(
                filter_name, filter_lookup, str_value,
            ))

    @classmethod
    def _reflect_like_value(cls, str_value):
        star_replacer = uuid4().hex
        return '\\'.join(
            v.replace(r'\{}'.format(RQL_ANY_SYMBOL), star_replacer)
            for v in cls.remove_quotes(str_value).split(r'\\')
        ), star_replacer

    @classmethod
    def _get_searching_typed_value(cls, django_lookup, str_value):
        val, star_replacer = cls._reflect_like_value(str_value)

        if '{}{}'.format(RQL_ANY_SYMBOL, RQL_ANY_SYMBOL) in val:
            raise ValueError

        if django_lookup not in (DjangoLookups.REGEX, DjangoLookups.I_REGEX):
            return val.replace(RQL_ANY_SYMBOL, '').replace(star_replacer, RQL_ANY_SYMBOL)

        any_symbol_regex = '(.*?)'
        if val == RQL_ANY_SYMBOL:
            return any_symbol_regex

        new_val = val
        new_val = new_val[1:] if val[0] == RQL_ANY_SYMBOL else '^{}'.format(new_val)
        new_val = new_val[:-1] if val[-1] == RQL_ANY_SYMBOL else '{}$'.format(new_val)
        return new_val.replace(RQL_ANY_SYMBOL, any_symbol_regex).replace(
            star_replacer, RQL_ANY_SYMBOL,
        )

    @classmethod
    def _convert_value(cls, django_field, str_value, use_repr=False):
        val = cls.remove_quotes(str_value)
        filter_type = FilterTypes.field_filter_type(django_field)

        if filter_type == FilterTypes.FLOAT:
            return float(val)

        elif filter_type == FilterTypes.DECIMAL:
            return round(float(val), django_field.decimal_places)

        elif filter_type == FilterTypes.DATE:
            dt = parse_date(val)
            if dt is None:
                raise ValueError
        elif filter_type == FilterTypes.DATETIME:
            dt = parse_datetime(val)
            if dt is None:
                raise ValueError

        elif filter_type == FilterTypes.BOOLEAN:
            if val not in (RQL_FALSE, RQL_TRUE):
                raise ValueError
            return val == RQL_TRUE

        if val == RQL_EMPTY:
            if (filter_type == FilterTypes.INT) or (not django_field.blank):
                raise ValueError
            return ''

        choices = getattr(django_field, 'choices', None)
        if not choices:
            if filter_type == FilterTypes.INT:
                return int(val)
            return val

        return cls._get_choices_field_db_value(val, choices, filter_type, use_repr)

    @classmethod
    def _get_choices_field_db_value(cls, value, choices, filter_type, use_repr):
        if type(choices).__name__ == 'Choices':
            return cls._get_choice_class_db_value(value, choices, filter_type, use_repr)

        # `use_repr=True` makes it possible to map choice representations to real db values
        # F.e.: `choices=((0, 'v0'), (1, 'v1'))` can be filtered by 'v1' if `use_repr=True` or
        # by '1' if `use_repr=False`
        if isinstance(choices[0], tuple):
            iterator = iter(
                choice[0] for choice in choices if str(choice[int(use_repr)]) == value
            )
        else:
            iterator = iter(choice for choice in choices if choice == value)
        try:
            db_value = next(iterator)
            return db_value
        except StopIteration:
            raise ValueError

    @staticmethod
    def _get_choice_class_db_value(value, choices, filter_type, use_repr):
        if use_repr:
            try:
                db_value = next(
                    db_value for db_value, value_repr in choices if value_repr == value
                )
                return db_value
            except StopIteration:
                raise ValueError

        if filter_type == FilterTypes.INT:
            db_value = int(value)
        else:
            db_value = value

        if db_value not in choices:
            raise ValueError

        return db_value

    @staticmethod
    def _build_django_q(filter_item, django_lookup, filter_lookup, typed_value):
        kwargs = {'{}__{}'.format(filter_item['orm_route'], django_lookup): typed_value}
        return ~Q(**kwargs) if filter_lookup == FilterLookups.NE else Q(**kwargs)

    @staticmethod
    def _get_filter_lookup_by_operator(grammar_operator):
        mapper = {
            ComparisonOperators.EQ: FilterLookups.EQ,
            ComparisonOperators.NE: FilterLookups.NE,
            ComparisonOperators.LT: FilterLookups.LT,
            ComparisonOperators.LE: FilterLookups.LE,
            ComparisonOperators.GT: FilterLookups.GT,
            ComparisonOperators.GE: FilterLookups.GE,
            SearchOperators.LIKE: FilterLookups.LIKE,
            SearchOperators.I_LIKE: FilterLookups.I_LIKE,
        }
        return mapper[grammar_operator]

    @staticmethod
    def _get_error_details(filter_name, filter_lookup, str_value):
        return {
            'details': {
                'filter': filter_name,
                'lookup': filter_lookup,
                'value': str_value,
            },
        }

    @staticmethod
    def remove_quotes(str_value):
        # Values can start with single or double quotes, if they have special chars inside them
        return str_value[1:-1] if str_value and str_value[0] in ('"', "'") else str_value

    @staticmethod
    def _is_searching_lookup(filter_lookup):
        return filter_lookup in (FilterLookups.LIKE, FilterLookups.I_LIKE)

    @staticmethod
    def _check_use_repr(filter_item, filter_name):
        assert not (filter_item.get('use_repr') and filter_item.get('ordering')), \
            "{}: 'use_repr' and 'ordering' can't be used together.".format(filter_name)
        assert not (filter_item.get('use_repr') and filter_item.get('search')), \
            "{}: 'use_repr' and 'search' can't be used together.".format(filter_name)

    @staticmethod
    def _check_dynamic(filter_item, filter_name, filter_route):
        field = filter_item.get('field')
        if filter_item.get('dynamic', False):
            assert filter_route == '', \
                "{}: dynamic filters are not supported in namespaces.".format(filter_name)
            assert field is not None, \
                "{}: dynamic filters must have 'field' set.".format(filter_name)
        else:
            assert not filter_item.get('custom', False) and field is None, \
                "{}: common filters can't have 'field' set.".format(filter_name)

    @staticmethod
    def _check_search(filter_item, filter_name, field):
        is_non_string_field_type = FilterTypes.field_filter_type(field) != FilterTypes.STRING
        assert not (filter_item.get('search') and is_non_string_field_type), \
            "{}: 'search' can be applied only to text filters.".format(filter_name)
