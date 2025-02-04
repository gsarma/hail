import abc
import json
import math
from collections.abc import Mapping, Sequence

import numpy as np

import hail as hl
from hail import genetics
from hail.expr.nat import NatBase, NatLiteral
from hail.expr.type_parsing import type_grammar, type_node_visitor
from hail.genetics.reference_genome import reference_genome_type
from hail.typecheck import *
from hail.utils.java import scala_object, jset, Env, escape_parsable

__all__ = [
    'dtype',
    'HailType',
    'hail_type',
    'is_container',
    'is_compound',
    'is_numeric',
    'is_primitive',
    'types_match',
    'tint',
    'tint32',
    'tint64',
    'tfloat',
    'tfloat32',
    'tfloat64',
    'tstr',
    'tbool',
    'tarray',
    'tndarray',
    'tset',
    'tdict',
    'tstruct',
    'tunion',
    'ttuple',
    'tinterval',
    'tlocus',
    'tcall',
    'tvoid',
    'tvariable',
    'hts_entry_schema',
]

def summary_type(t):
    if isinstance(t, hl.tdict):
        return f'dict<{summary_type(t.key_type)}, {summary_type(t.value_type)}>'
    elif isinstance(t, hl.tset):
        return f'set<{summary_type(t.element_type)}>'
    elif isinstance(t, hl.tarray):
        return f'array<{summary_type(t.element_type)}>'
    elif isinstance(t, hl.tstruct):
        return f'struct with {len(t)} fields'
    elif isinstance(t, hl.ttuple):
        return f'tuple with {len(t)} fields'
    elif isinstance(t, hl.tinterval):
        return f'interval<{summary_type(t.point_type)}>'
    else:
        return str(t)

def dtype(type_str):
    r"""Parse a type from its string representation.

    Examples
    --------

    >>> hl.dtype('int')
    dtype('int32')

    >>> hl.dtype('float')
    dtype('float64')

    >>> hl.dtype('array<int32>')
    dtype('array<int32>')

    >>> hl.dtype('dict<str, bool>')
    dtype('dict<str, bool>')

    >>> hl.dtype('struct{a: int32, `field with spaces`: int64}')
    dtype('struct{a: int32, `field with spaces`: int64}')

    Notes
    -----
    This function is able to reverse ``str(t)`` on a :class:`.HailType`.

    The grammar is defined as follows:

    .. code-block:: text

        type = _ (array / set / dict / struct / union / tuple / interval / int64 / int32 / float32 / float64 / bool / str / call / str / locus) _
        int64 = "int64" / "tint64"
        int32 = "int32" / "tint32" / "int" / "tint"
        float32 = "float32" / "tfloat32"
        float64 = "float64" / "tfloat64" / "tfloat" / "float"
        bool = "tbool" / "bool"
        call = "tcall" / "call"
        str = "tstr" / "str"
        locus = ("tlocus" / "locus") _ "[" identifier "]"
        array = ("tarray" / "array") _ "<" type ">"
        ndarray = ("tndarray" / "ndarray") _ "<" type, identifier ">"
        set = ("tset" / "set") _ "<" type ">"
        dict = ("tdict" / "dict") _ "<" type "," type ">"
        struct = ("tstruct" / "struct") _ "{" (fields / _) "}"
        union = ("tunion" / "union") _ "{" (fields / _) "}"
        tuple = ("ttuple" / "tuple") _ "(" ((type ("," type)*) / _) ")"
        fields = field ("," field)*
        field = identifier ":" type
        interval = ("tinterval" / "interval") _ "<" type ">"
        identifier = _ (simple_identifier / escaped_identifier) _
        simple_identifier = ~"\w+"
        escaped_identifier = ~"`([^`\\\\]|\\\\.)*`"
        _ = ~"\s*"

    Parameters
    ----------
    type_str : :obj:`str`
        String representation of type.

    Returns
    -------
    :class:`.HailType`
    """
    tree = type_grammar.parse(type_str)
    return type_node_visitor.visit(tree)


class HailTypeContext(object):
    def __init__(self, references=set()):
        self.references = references

    @property
    def is_empty(self):
        return len(self.references) == 0

    def _to_json_context(self):
        if self._json is None:
            self._json = {
                'reference_genomes':
                    {r: hl.get_reference(r)._config for r in self.references}
            }
        return self._json

    @classmethod
    def union(cls, *types):
        ctxs = [t.get_context() for t in types if not t.get_context().is_empty]
        if len(ctxs) == 0:
            return _empty_context
        if len(ctxs) == 1:
            return ctxs[0]
        refs = ctxs[0].references.union(*[ctx.references for ctx in ctxs[1:]])
        return HailTypeContext(refs)


_empty_context = HailTypeContext()


class HailType(object):
    """
    Hail type superclass.
    """

    def __init__(self):
        super(HailType, self).__init__()
        self._context = None

    def __repr__(self):
        s = str(self).replace("'", "\\'")
        return "dtype('{}')".format(s)

    @abc.abstractmethod
    def _eq(self, other):
        return

    def __eq__(self, other):
        return isinstance(other, HailType) and self._eq(other)

    @abc.abstractmethod
    def __str__(self):
        return

    def __hash__(self):
        # FIXME this is a bit weird
        return 43 + hash(str(self))

    def pretty(self, indent=0, increment=4):
        """Returns a prettily formatted string representation of the type.

        Parameters
        ----------
        indent : :obj:`int`
            Spaces to indent.

        Returns
        -------
        :obj:`str`
        """
        l = []
        l.append(' ' * indent)
        self._pretty(l, indent, increment)
        return ''.join(l)

    def _pretty(self, l, indent, increment):
        l.append(str(self))

    @abc.abstractmethod
    def _parsable_string(self):
        pass

    def typecheck(self, value):
        """Check that `value` matches a type.

        Parameters
        ----------
        value
            Value to check.

        Raises
        ------
        :obj:`TypeError`
        """
        def check(t, obj):
            t._typecheck_one_level(obj)
            return True
        self._traverse(value, check)

    @abc.abstractmethod
    def _typecheck_one_level(self, annotation):
        pass

    def _to_json(self, x):
        converted = self._convert_to_json_na(x)
        return json.dumps(converted)

    def _convert_to_json_na(self, x):
        if x is None:
            return x
        else:
            return self._convert_to_json(x)

    def _convert_to_json(self, x):
        return x

    def _from_json(self, s):
        x = json.loads(s)
        return self._convert_from_json_na(x)

    def _convert_from_json_na(self, x):
        if x is None:
            return x
        else:
            return self._convert_from_json(x)

    def _convert_from_json(self, x):
        return x


    def _traverse(self, obj, f):
        """Traverse a nested type and object.

        Parameters
        ----------
        obj : Any
        f : Callable[[HailType, Any], bool]
            Function to evaluate on the type and object. Traverse children if
            the function returns ``True``.
        """
        f(self, obj)

    @abc.abstractmethod
    def unify(self, t):
        raise NotImplementedError

    @abc.abstractmethod
    def subst(self):
        raise NotImplementedError

    @abc.abstractmethod
    def clear(self):
        raise NotImplementedError

    def _get_context(self):
        return _empty_context

    def get_context(self):
        if self._context is None:
            self._context = self._get_context()
        return self._context


hail_type = oneof(HailType, transformed((str, dtype)))


class _tvoid(HailType):
    def __init__(self):
        super(_tvoid, self).__init__()

    def __str__(self):
        return "void"

    def _eq(self, other):
        return isinstance(other, _tvoid)

    def _parsable_string(self):
        return "Void"

    def unify(self, t):
        return t == tvoid

    def subst(self):
        return self

    def clear(self):
        pass

class _tint32(HailType):
    """Hail type for signed 32-bit integers.

    Their values can range from :math:`-2^{31}` to :math:`2^{31} - 1`
    (approximately 2.15 billion).

    In Python, these are represented as :obj:`int`.
    """

    def __init__(self):
        super(_tint32, self).__init__()

    def _typecheck_one_level(self, annotation):
        if annotation is not None:
            if not isinstance(annotation, int):
                raise TypeError("type 'tint32' expected Python 'int', but found type '%s'" % type(annotation))
            elif not self.min_value <= annotation <= self.max_value:
                raise TypeError(f"Value out of range for 32-bit integer: "
                                f"expected [{self.min_value}, {self.max_value}], found {annotation}")

    def __str__(self):
        return "int32"

    def _eq(self, other):
        return isinstance(other, _tint32)

    def _parsable_string(self):
        return "Int32"

    @property
    def min_value(self):
        return -(1 << 31)

    @property
    def max_value(self):
        return (1 << 31) - 1

    def unify(self, t):
        return t == tint32

    def subst(self):
        return self

    def clear(self):
        pass

    def to_numpy(self):
        return np.int32


class _tint64(HailType):
    """Hail type for signed 64-bit integers.

    Their values can range from :math:`-2^{63}` to :math:`2^{63} - 1`.

    In Python, these are represented as :obj:`int`.
    """

    def __init__(self):
        super(_tint64, self).__init__()

    def _typecheck_one_level(self, annotation):
        if annotation is not None:
            if not isinstance(annotation, int):
                raise TypeError("type 'int64' expected Python 'int', but found type '%s'" % type(annotation))
            if not self.min_value <= annotation <= self.max_value:
                raise TypeError(f"Value out of range for 64-bit integer: "
                                f"expected [{self.min_value}, {self.max_value}], found {annotation}")

    def __str__(self):
        return "int64"

    def _eq(self, other):
        return isinstance(other, _tint64)

    def _parsable_string(self):
        return "Int64"

    @property
    def min_value(self):
        return -(1 << 63)

    @property
    def max_value(self):
        return (1 << 63) - 1

    def unify(self, t):
        return t == tint64

    def subst(self):
        return self

    def clear(self):
        pass

    def to_numpy(self):
        return np.int64


class _tfloat32(HailType):
    """Hail type for 32-bit floating point numbers.

    In Python, these are represented as :obj:`float`.
    """

    def __init__(self):
        super(_tfloat32, self).__init__()

    def _typecheck_one_level(self, annotation):
        if annotation is not None and not isinstance(annotation, (float, int)):
            raise TypeError("type 'float32' expected Python 'float', but found type '%s'" % type(annotation))

    def __str__(self):
        return "float32"

    def _eq(self, other):
        return isinstance(other, _tfloat32)

    def _parsable_string(self):
        return "Float32"

    def _convert_from_json(self, x):
        return float(x)

    def _convert_to_json(self, x):
        if math.isfinite(x):
            return x
        else:
            return str(x)

    def unify(self, t):
        return t == tfloat32

    def subst(self):
        return self

    def clear(self):
        pass

    def to_numpy(self):
        return np.float32


class _tfloat64(HailType):
    """Hail type for 64-bit floating point numbers.

    In Python, these are represented as :obj:`float`.
    """

    def __init__(self):
        super(_tfloat64, self).__init__()

    def _typecheck_one_level(self, annotation):
        if annotation is not None and not isinstance(annotation, (float, int)):
            raise TypeError("type 'float64' expected Python 'float', but found type '%s'" % type(annotation))
    def __str__(self):
        return "float64"

    def _eq(self, other):
        return isinstance(other, _tfloat64)

    def _parsable_string(self):
        return "Float64"

    def _convert_from_json(self, x):
        return float(x)

    def _convert_to_json(self, x):
        if math.isfinite(x):
            return x
        else:
            return str(x)

    def unify(self, t):
        return t == tfloat64

    def subst(self):
        return self

    def clear(self):
        pass

    def to_numpy(self):
        return np.float64


class _tstr(HailType):
    """Hail type for text strings.

    In Python, these are represented as strings.
    """

    def __init__(self):
        super(_tstr, self).__init__()

    def _typecheck_one_level(self, annotation):
        if annotation and not isinstance(annotation, str):
            raise TypeError("type 'str' expected Python 'str', but found type '%s'" % type(annotation))

    def __str__(self):
        return "str"

    def _eq(self, other):
        return isinstance(other, _tstr)

    def _parsable_string(self):
        return "String"

    def unify(self, t):
        return t == tstr

    def subst(self):
        return self

    def clear(self):
        pass


class _tbool(HailType):
    """Hail type for Boolean (``True`` or ``False``) values.

    In Python, these are represented as :obj:`bool`.
    """

    def __init__(self):
        super(_tbool, self).__init__()

    def _typecheck_one_level(self, annotation):
        if annotation is not None and not isinstance(annotation, bool):
            raise TypeError("type 'bool' expected Python 'bool', but found type '%s'" % type(annotation))

    def __str__(self):
        return "bool"

    def _eq(self, other):
        return isinstance(other, _tbool)

    def _parsable_string(self):
        return "Boolean"

    def unify(self, t):
        return t == tbool

    def subst(self):
        return self

    def clear(self):
        pass

    def to_numpy(self):
        return np.bool


class tndarray(HailType):
    """Hail type for n-dimensional arrays.

    .. include:: _templates/experimental.rst

    In Python, these are represented as NumPy :obj:`ndarray`.

    Notes
    -----

    NDArrays contain elements of only one type, which is parameterized by
    `element_type`.

    Parameters
    ----------
    element_type : :class:`.HailType`
        Element type of array.
    ndim : int32
        Number of dimensions.

    See Also
    --------
    :class:`.NDArrayExpression`, :func:`.ndarray`
    """

    @typecheck_method(element_type=hail_type, ndim=oneof(NatBase, int))
    def __init__(self, element_type, ndim):
        self._element_type = element_type
        self._ndim = NatLiteral(ndim) if isinstance(ndim, int) else ndim
        super(tndarray, self).__init__()

    @property
    def element_type(self):
        """NDArray element type.

        Returns
        -------
        :class:`.HailType`
            Element type.
        """
        return self._element_type

    @property
    def ndim(self):
        """NDArray number of dimensions.

        Returns
        -------
        :obj:`int`
            Number of dimensions.
        """
        assert isinstance(self._ndim, NatLiteral), "tndarray must be realized with a concrete number of dimensions"
        return self._ndim.n

    def _traverse(self, obj, f):
        if f(self, obj):
            for elt in np.nditer(obj):
                self.element_type._traverse(elt.item(), f)

    def _typecheck_one_level(self, annotation):
        if annotation is not None and not isinstance(annotation, np.ndarray):
            raise TypeError("type 'ndarray' expected Python 'numpy.ndarray', but found type '%s'" % type(annotation))

    def __str__(self):
        return "ndarray<{}, {}>".format(self.element_type, self.ndim)

    def _eq(self, other):
        return isinstance(other, tndarray) and self.element_type == other.element_type

    def _pretty(self, l, indent, increment):
        l.append('ndarray<')
        self._element_type._pretty(l, indent, increment)
        l.append(', ')
        l.append(str(self.ndim))
        l.append('>')

    def _parsable_string(self):
        return f'NDArray[{self._element_type._parsable_string()},{self.ndim}]'

    def _convert_from_json(self, x):
        np_type = self.element_type.to_numpy()
        return np.ndarray(shape=x['shape'], buffer=np.array(x['data'], dtype=np_type), strides=x['strides'], dtype=np_type)

    def _convert_to_json(self, x):
        data = x.reshape(x.size).tolist()
        json_dict = {
            "shape": x.shape,
            "strides": x.strides,
            "flags": 0,
            "data": data,
            "offset": 0
        }
        return json_dict

    def clear(self):
        self._element_type.clear()
        self._ndim.clear()

    def unify(self, t):
        return isinstance(t, tndarray) and \
               self._element_type.unify(t._element_type) and \
               self._ndim.unify(t._ndim)

    def subst(self):
        return tndarray(self._element_type.subst(), self._ndim.subst())

    def _get_context(self):
        return self.element_type.get_context()


class tarray(HailType):
    """Hail type for variable-length arrays of elements.

    In Python, these are represented as :obj:`list`.

    Notes
    -----
    Arrays contain elements of only one type, which is parameterized by
    `element_type`.

    Parameters
    ----------
    element_type : :class:`.HailType`
        Element type of array.

    See Also
    --------
    :class:`.ArrayExpression`, :class:`.CollectionExpression`,
    :func:`.array`, :ref:`sec-collection-functions`
    """

    @typecheck_method(element_type=hail_type)
    def __init__(self, element_type):
        self._element_type = element_type
        super(tarray, self).__init__()

    @property
    def element_type(self):
        """Array element type.

        Returns
        -------
        :class:`.HailType`
            Element type.
        """
        return self._element_type

    def _traverse(self, obj, f):
        if f(self, obj):
            for elt in obj:
                self.element_type._traverse(elt, f)

    def _typecheck_one_level(self, annotation):
        if annotation is not None:
            if not isinstance(annotation, Sequence):
                raise TypeError("type 'array' expected Python 'list', but found type '%s'" % type(annotation))

    def __str__(self):
        return "array<{}>".format(self.element_type)

    def _eq(self, other):
        return isinstance(other, tarray) and self.element_type == other.element_type

    def _pretty(self, l, indent, increment):
        l.append('array<')
        self.element_type._pretty(l, indent, increment)
        l.append('>')

    def _parsable_string(self):
        return "Array[" + self.element_type._parsable_string() + "]"

    def _convert_from_json(self, x):
        return [self.element_type._convert_from_json_na(elt) for elt in x]

    def _convert_to_json(self, x):
        return [self.element_type._convert_to_json_na(elt) for elt in x]

    def _propagate_jtypes(self, jtype):
        self._element_type._add_jtype(jtype.elementType())

    def unify(self, t):
        return isinstance(t, tarray) and self.element_type.unify(t.element_type)

    def subst(self):
        return tarray(self.element_type.subst())

    def clear(self):
        self.element_type.clear()

    def _get_context(self):
        return self.element_type.get_context()


class tset(HailType):
    """Hail type for collections of distinct elements.

    In Python, these are represented as :obj:`set`.

    Notes
    -----
    Sets contain elements of only one type, which is parameterized by
    `element_type`.

    Parameters
    ----------
    element_type : :class:`.HailType`
        Element type of set.

    See Also
    --------
    :class:`.SetExpression`, :class:`.CollectionExpression`,
    :func:`.set`, :ref:`sec-collection-functions`
    """

    @typecheck_method(element_type=hail_type)
    def __init__(self, element_type):
        self._element_type = element_type
        super(tset, self).__init__()

    @property
    def element_type(self):
        """Set element type.

        Returns
        -------
        :class:`.HailType`
            Element type.
        """
        return self._element_type

    def _traverse(self, obj, f):
        if f(self, obj):
            for elt in obj:
                self.element_type._traverse(elt, f)

    def _typecheck_one_level(self, annotation):
        if annotation is not None:
            if not isinstance(annotation, set):
                raise TypeError("type 'set' expected Python 'set', but found type '%s'" % type(annotation))

    def __str__(self):
        return "set<{}>".format(self.element_type)

    def _eq(self, other):
        return isinstance(other, tset) and self.element_type == other.element_type

    def _pretty(self, l, indent, increment):
        l.append('set<')
        self.element_type._pretty(l, indent, increment)
        l.append('>')

    def _parsable_string(self):
        return "Set[" + self.element_type._parsable_string() + "]"

    def _convert_from_json(self, x):
        return {self.element_type._convert_from_json_na(elt) for elt in x}

    def _convert_to_json(self, x):
        return [self.element_type._convert_to_json_na(elt) for elt in x]

    def _propagate_jtypes(self, jtype):
        self._element_type._add_jtype(jtype.elementType())

    def unify(self, t):
        return isinstance(t, tset) and self.element_type.unify(t.element_type)

    def subst(self):
        return tset(self.element_type.subst())

    def clear(self):
        self.element_type.clear()

    def _get_context(self):
        return self.element_type.get_context()


class tdict(HailType):
    """Hail type for key-value maps.

    In Python, these are represented as :obj:`dict`.

    Notes
    -----
    Dicts parameterize the type of both their keys and values with
    `key_type` and `value_type`.

    Parameters
    ----------
    key_type: :class:`.HailType`
        Key type.
    value_type: :class:`.HailType`
        Value type.

    See Also
    --------
    :class:`.DictExpression`, :func:`.dict`, :ref:`sec-collection-functions`
    """

    @typecheck_method(key_type=hail_type, value_type=hail_type)
    def __init__(self, key_type, value_type):
        self._key_type = key_type
        self._value_type = value_type
        super(tdict, self).__init__()

    @property
    def key_type(self):
        """Dict key type.

        Returns
        -------
        :class:`.HailType`
            Key type.
        """
        return self._key_type

    @property
    def value_type(self):
        """Dict value type.

        Returns
        -------
        :class:`.HailType`
            Value type.
        """
        return self._value_type

    @property
    def element_type(self):
        return tstruct(key = self._key_type, value = self._value_type)

    def _traverse(self, obj, f):
        if f(self, obj):
            for k, v in obj.items():
                self.key_type._traverse(k, f)
                self.value_type._traverse(v, f)

    def _typecheck_one_level(self, annotation):
        if annotation is not None:
            if not isinstance(annotation, dict):
                raise TypeError("type 'dict' expected Python 'dict', but found type '%s'" % type(annotation))

    def __str__(self):
        return "dict<{}, {}>".format(self.key_type, self.value_type)

    def _eq(self, other):
        return isinstance(other, tdict) and self.key_type == other.key_type and self.value_type == other.value_type

    def _pretty(self, l, indent, increment):
        l.append('dict<')
        self.key_type._pretty(l, indent, increment)
        l.append(', ')
        self.value_type._pretty(l, indent, increment)
        l.append('>')

    def _parsable_string(self):
        return "Dict[{},{}]".format(self.key_type._parsable_string(), self.value_type._parsable_string())

    def _convert_from_json(self, x):
        return {self.key_type._convert_from_json_na(elt['key']): self.value_type._convert_from_json_na(elt['value']) for
                elt in x}

    def _convert_to_json(self, x):
        return [{'key': self.key_type._convert_to_json(k),
                 'value':self.value_type._convert_to_json(v)} for k, v in x.items()]

    def _propagate_jtypes(self, jtype):
        self._key_type._add_jtype(jtype.keyType())
        self._value_type._add_jtype(jtype.valueType())

    def unify(self, t):
        return (isinstance(t, tdict)
                and self.key_type.unify(t.key_type)
                and self.value_type.unify(t.value_type))

    def subst(self):
        return tdict(self._key_type.subst(), self._value_type.subst())

    def clear(self):
        self.key_type.clear()
        self.value_type.clear()

    def _get_context(self):
        return HailTypeContext.union(self.key_type, self.value_type)


class tstruct(HailType, Mapping):
    """Hail type for structured groups of heterogeneous fields.

    In Python, these are represented as :class:`.Struct`.

    Parameters
    ----------
    field_types : keyword args of :class:`.HailType`
        Fields.

    See Also
    --------
    :class:`.StructExpression`, :class:`.Struct`
    """

    @typecheck_method(field_types=hail_type)
    def __init__(self, **field_types):
        self._field_types = field_types
        self._fields = tuple(field_types)
        super(tstruct, self).__init__()

    @property
    def types(self):
        """Struct field types.

        Returns
        -------
        :obj:`tuple` of :class:`.HailType`
        """
        return tuple(self._field_types.values())

    @property
    def fields(self):
        """Struct field names.

        Returns
        -------
        :obj:`tuple` of :obj:`str`
            Tuple of struct field names.
        """
        return self._fields

    def _traverse(self, obj, f):
        if f(self, obj):
            for k, v in obj.items():
                t = self[k]
                t._traverse(v, f)

    def _typecheck_one_level(self, annotation):
        if annotation:
            if isinstance(annotation, Mapping):
                s = set(self)
                for f in annotation:
                    if f not in s:
                        raise TypeError("type '%s' expected fields '%s', but found fields '%s'" %
                                        (self, list(self), list(annotation)))
            else:
                raise TypeError("type 'struct' expected type Mapping (e.g. dict or hail.utils.Struct), but found '%s'" %
                                type(annotation))

    @typecheck_method(item=oneof(int, str))
    def __getitem__(self, item):
        if not isinstance(item, str):
            item = self._fields[item]
        return self._field_types[item]

    def __iter__(self):
        return iter(self._field_types)

    def __len__(self):
        return len(self._fields)

    def __str__(self):
        return "struct{{{}}}".format(
            ', '.join('{}: {}'.format(escape_parsable(f), str(t)) for f, t in self.items()))

    def _eq(self, other):
        return (isinstance(other, tstruct)
                and self._fields == other._fields
                and all(self[f] == other[f] for f in self._fields))

    def _pretty(self, l, indent, increment):
        if not self._fields:
            l.append('struct {}')
            return

        pre_indent = indent
        indent += increment
        l.append('struct {')
        for i, (f, t) in enumerate(self.items()):
            if i > 0:
                l.append(', ')
            l.append('\n')
            l.append(' ' * indent)
            l.append('{}: '.format(escape_parsable(f)))
            t._pretty(l, indent, increment)
        l.append('\n')
        l.append(' ' * pre_indent)
        l.append('}')

    def _parsable_string(self):
        return "Struct{{{}}}".format(
            ','.join('{}:{}'.format(escape_parsable(f), t._parsable_string()) for f, t in self.items()))

    def _convert_from_json(self, x):
        from hail.utils import Struct
        return Struct(**{f: t._convert_from_json_na(x.get(f)) for f, t in self.items()})

    def _convert_to_json(self, x):
        return {f: t._convert_to_json_na(x[f]) for f, t in self.items()}

    def _is_prefix_of(self, other):
        return (isinstance(other, tstruct) and
                len(self._fields) <= len(other._fields) and
                all(x == y for x, y in zip(self._field_types.values(), other._field_types.values())))

    def _concat(self, other):
        new_field_types = {}
        new_field_types.update(self._field_types)
        new_field_types.update(other._field_types)
        return tstruct(**new_field_types)

    def _insert(self, path, t):
        if not path:
            return t

        key = path[0]
        keyt = self.get(key)
        if not (keyt and isinstance(keyt, tstruct)):
            keyt = tstruct()
        return self._insert_fields(**{key: keyt._insert(path[1:], t)})

    def _insert_field(self, field, typ):
        return self._insert_fields(**{field: typ})

    def _insert_fields(self, **new_fields):
        new_field_types = {}
        new_field_types.update(self._field_types)
        new_field_types.update(new_fields)
        return tstruct(**new_field_types)

    def _drop_fields(self, fields):
        return tstruct(**{f: t for f, t in self.items() if f not in fields})

    def _select_fields(self, fields):
        return tstruct(**{f: self[f] for f in fields})

    def _index_path(self, path):
        t = self
        for p in path:
            t = t[p]
        return t

    def _rename(self, map):
        seen = {}
        new_field_types = {}

        for f0, t in self.items():
            f = map.get(f0, f0)
            if f in seen:
                raise ValueError(
                    "Cannot rename two fields to the same name: attempted to rename {} and {} both to {}".format(
                        repr(seen[f]), repr(f0), repr(f)))
            else:
                seen[f] = f0
                new_field_types[f] = t

        return tstruct(**new_field_types)

    def unify(self, t):
        if not (isinstance(t, tstruct) and len(self) == len(t)):
            return False
        for (f1, t1), (f2, t2) in zip(self.items(), t.items()):
            if not (f1 == f2 and t1.unify(t2)):
                return False
        return True

    def subst(self):
        return tstruct(**{f: t.subst() for f, t in self.items()})

    def clear(self):
        for f, t in self.items():
            t.clear()

    def _get_context(self):
        return HailTypeContext.union(*self.values())

class tunion(HailType, Mapping):
    @typecheck_method(case_types=hail_type)
    def __init__(self, **case_types):
        """Tagged union type.  Values of type union represent one of several
        heterogenous, named cases.

        Parameters
        ----------
        cases : keyword args of :class:`.HailType`
            The union cases.

        """

        super(tunion, self).__init__()
        self._case_types = case_types
        self._cases = tuple(case_types)

    @property
    def cases(self):

        """Return union case names.

        Returns
        -------
        :obj:`tuple` of :obj:`str`
            Tuple of union case names
        """
        return self._cases

    @typecheck_method(item=oneof(int, str))
    def __getitem__(self, item):
        if isinstance(item, int):
            item = self._cases[item]
        return self._case_types[item]

    def __iter__(self):
        return iter(self._case_types)

    def __len__(self):
        return len(self._cases)

    def __str__(self):
        return "union{{{}}}".format(
            ', '.join('{}: {}'.format(escape_parsable(f), str(t)) for f, t in self.items()))

    def _eq(self, other):
        return (isinstance(other, tunion)
                and self._cases == other._cases
                and all(self[c] == other[c] for c in self._cases))

    def _pretty(self, l, indent, increment):
        if not self._cases:
            l.append('union {}')
            return

        pre_indent = indent
        indent += increment
        l.append('union {')
        for i, (f, t) in enumerate(self.items()):
            if i > 0:
                l.append(', ')
            l.append('\n')
            l.append(' ' * indent)
            l.append('{}: '.format(escape_parsable(f)))
            t._pretty(l, indent, increment)
        l.append('\n')
        l.append(' ' * pre_indent)
        l.append('}')

    def _parsable_string(self):
        return "Union{{{}}}".format(
            ','.join('{}:{}'.format(escape_parsable(f), t._parsable_string()) for f, t in self.items()))

    def unify(self, t):
        if not (isinstance(t, union) and len(self) == len(t)):
            return False
        for (f1, t1), (f2, t2) in zip(self.items(), t.items()):
            if not (f1 == f2 and t1.unify(t2)):
                return False
        return True

    def subst(self):
        return tunion(**{f: t.subst() for f, t in self.items()})

    def clear(self):
        for f, t in self.items():
            t.clear()

    def _get_context(self):
        return HailTypeContext.union(*self.values())


class ttuple(HailType, Sequence):
    """Hail type for tuples.

    In Python, these are represented as :obj:`tuple`.

    Parameters
    ----------
    types: varargs of :class:`.HailType`
        Element types.

    See Also
    --------
    :class:`.TupleExpression`
    """

    @typecheck_method(types=hail_type)
    def __init__(self, *types):
        self._types = types
        super(ttuple, self).__init__()

    @property
    def types(self):
        """Tuple element types.

        Returns
        -------
        :obj:`tuple` of :class:`.HailType`
        """
        return self._types

    def _traverse(self, obj, f):
        if f(self, obj):
            for t, elt in zip(self.types, obj):
                t._traverse(elt, f)

    def _typecheck_one_level(self, annotation):
        if annotation:
            if not isinstance(annotation, tuple):
                raise TypeError("type 'tuple' expected Python tuple, but found '%s'" %
                                type(annotation))
            if len(annotation) != len(self.types):
                raise TypeError("%s expected tuple of size '%i', but found '%s'" %
                                (self, len(self.types), annotation))

    @typecheck_method(item=int)
    def __getitem__(self, item):
        return self._types[item]

    def __iter__(self):
        for i in range(len(self)):
            yield self[i]

    def __len__(self):
        return len(self._types)

    def __str__(self):
        return "tuple({})".format(", ".join([str(t) for t in self.types]))

    def _eq(self, other):
        from operator import eq
        return isinstance(other, ttuple) and len(self.types) == len(other.types) and all(
            map(eq, self.types, other.types))

    def _pretty(self, l, indent, increment):
        pre_indent = indent
        indent += increment
        l.append('tuple (')
        for i, t in enumerate(self.types):
            if i > 0:
                l.append(', ')
            l.append('\n')
            l.append(' ' * indent)
            t._pretty(l, indent, increment)
        l.append('\n')
        l.append(' ' * pre_indent)
        l.append(')')

    def _parsable_string(self):
        return "Tuple[{}]".format(",".join([t._parsable_string() for t in self.types]))

    def _convert_from_json(self, x):
        return tuple(self.types[i]._convert_from_json_na(x[i]) for i in range(len(self.types)))

    def _convert_to_json(self, x):
        return [self.types[i]._convert_to_json_na(x[i]) for i in range(len(self.types))]

    def unify(self, t):
        if not (isinstance(t, ttuple) and len(self.types) == len(t.types)):
            return False
        for t1, t2 in zip(self.types, t.types):
            if not t1.unify(t2):
                return False
        return True

    def subst(self):
        return ttuple(*[t.subst() for t in self.types])

    def clear(self):
        for t in self.types:
            t.clear()

    def _get_context(self):
        return HailTypeContext.union(*self.types)


class _tcall(HailType):
    """Hail type for a diploid genotype.

    In Python, these are represented by :class:`.Call`.
    """

    def __init__(self):
        super(_tcall, self).__init__()

    def _typecheck_one_level(self, annotation):
        if annotation is not None and not isinstance(annotation, genetics.Call):
            raise TypeError("type 'call' expected Python hail.genetics.Call, but found %s'" %
                            type(annotation))

    def __str__(self):
        return "call"

    def _eq(self, other):
        return isinstance(other, _tcall)

    def _parsable_string(self):
        return "Call"

    def _convert_from_json(self, x):
        return hl.Call._from_java(hl.Call._call_jobject().parse(x))

    def _convert_to_json(self, x):
        return str(x)

    def unify(self, t):
        return t == tcall

    def subst(self):
        return self

    def clear(self):
        pass


class tlocus(HailType):
    """Hail type for a genomic coordinate with a contig and a position.

    In Python, these are represented by :class:`.Locus`.

    Parameters
    ----------
    reference_genome: :class:`.ReferenceGenome` or :obj:`str`
        Reference genome to use.

    See Also
    --------
    :class:`.LocusExpression`, :func:`.locus`, :func:`.parse_locus`,
    :class:`.Locus`
    """

    @typecheck_method(reference_genome=reference_genome_type)
    def __init__(self, reference_genome='default'):
        self._rg = reference_genome
        super(tlocus, self).__init__()

    def _typecheck_one_level(self, annotation):
        if annotation is not None:
            if not isinstance(annotation, genetics.Locus):
                raise TypeError("type '{}' expected Python hail.genetics.Locus, but found '{}'"
                                .format(self, type(annotation)))
            if not self.reference_genome == annotation.reference_genome:
                raise TypeError("type '{}' encountered Locus with reference genome {}"
                                .format(self, repr(annotation.reference_genome)))

    def __str__(self):
        return "locus<{}>".format(escape_parsable(str(self.reference_genome)))

    def _parsable_string(self):
        return "Locus({})".format(escape_parsable(str(self.reference_genome)))

    def _eq(self, other):
        return isinstance(other, tlocus) and self.reference_genome == other.reference_genome

    @property
    def reference_genome(self):
        """Reference genome.

        Returns
        -------
        :class:`.ReferenceGenome`
            Reference genome.
        """
        if self._rg is None:
            self._rg = hl.default_reference()
        return self._rg

    def _pretty(self, l, indent, increment):
        l.append('locus<{}>'.format(escape_parsable(self.reference_genome.name)))

    def _convert_from_json(self, x):
        return genetics.Locus(x['contig'], x['position'], reference_genome=self.reference_genome)

    def _convert_to_json(self, x):
        return {'contig': x.contig, 'position': x.position}

    def unify(self, t):
        return isinstance(t, tlocus) and self.reference_genome == t.reference_genome

    def subst(self):
        return self

    def clear(self):
        pass

    def _get_context(self):
        return HailTypeContext(references={self.reference_genome.name})


class tinterval(HailType):
    """Hail type for intervals of ordered values.

    In Python, these are represented by :class:`.Interval`.

    Parameters
    ----------
    point_type: :class:`.HailType`
        Interval point type.

    See Also
    --------
    :class:`.IntervalExpression`, :class:`.Interval`, :func:`.interval`,
    :func:`.parse_locus_interval`
    """

    @typecheck_method(point_type=hail_type)
    def __init__(self, point_type):
        self._point_type = point_type
        super(tinterval, self).__init__()

    @property
    def point_type(self):
        """Interval point type.

        Returns
        -------
        :class:`.HailType`
            Interval point type.
        """
        return self._point_type

    def _traverse(self, obj, f):
        if f(self, obj):
            self.point_type._traverse(obj.start, f)
            self.point_type._traverse(obj.end, f)

    def _typecheck_one_level(self, annotation):
        from hail.utils import Interval
        if annotation is not None:
            if not isinstance(annotation, Interval):
                raise TypeError("type '{}' expected Python hail.utils.Interval, but found {}"
                                .format(self, type(annotation)))
            if annotation.point_type != self.point_type:
                raise TypeError("type '{}' encountered Interval with point type {}"
                                .format(self, repr(annotation.point_type)))

    def __str__(self):
        return "interval<{}>".format(str(self.point_type))

    def _eq(self, other):
        return isinstance(other, tinterval) and self.point_type == other.point_type

    def _pretty(self, l, indent, increment):
        l.append('interval<')
        self.point_type._pretty(l, indent, increment)
        l.append('>')

    def _parsable_string(self):
        return "Interval[{}]".format(self.point_type._parsable_string())

    def _convert_from_json(self, x):
        from hail.utils import Interval
        return Interval(self.point_type._convert_from_json_na(x['start']),
                        self.point_type._convert_from_json_na(x['end']),
                        x['includeStart'],
                        x['includeEnd'])

    def _convert_to_json(self, x):
        return {'start': self.point_type._convert_to_json_na(x.start),
                'end': self.point_type._convert_to_json_na(x.end),
                'includeStart': x.includes_start,
                'includeEnd': x.includes_end}

    def unify(self, t):
        return isinstance(t, tinterval) and self.point_type.unify(t.point_type)

    def subst(self):
        return tinterval(self.point_type.subst())

    def clear(self):
        self.point_type.clear()

    def _get_context(self):
        return self.point_type.get_context()


class Box(object):
    named_boxes = {}

    @staticmethod
    def from_name(name):
        if name in Box.named_boxes:
            return Box.named_boxes[name]
        b = Box()
        Box.named_boxes[name] = b
        return b

    def __init__(self):
        pass

    def unify(self, v):
        if hasattr(self, 'value'):
            return self.value == v
        self.value = v
        return True

    def clear(self):
        if hasattr(self, 'value'):
            del self.value

    def get(self):
        assert hasattr(self, 'value')
        return self.value


tvoid = _tvoid()


tint32 = _tint32()
"""Hail type for signed 32-bit integers.

Their values can range from :math:`-2^{31}` to :math:`2^{31} - 1`
(approximately 2.15 billion).

In Python, these are represented as :obj:`int`.

See Also
--------
:class:`.Int32Expression`, :func:`.int`, :func:`.int32`
"""


tint64 = _tint64()
"""Hail type for signed 64-bit integers.

Their values can range from :math:`-2^{63}` to :math:`2^{63} - 1`.

In Python, these are represented as :obj:`int`.

See Also
--------
:class:`.Int64Expression`, :func:`.int64`
"""

tint = tint32
"""Alias for :py:data:`.tint32`."""

tfloat32 = _tfloat32()
"""Hail type for 32-bit floating point numbers.

In Python, these are represented as :obj:`float`.

See Also
--------
:class:`.Float32Expression`, :func:`.float64`
"""

tfloat64 = _tfloat64()
"""Hail type for 64-bit floating point numbers.

In Python, these are represented as :obj:`float`.

See Also
--------
:class:`.Float64Expression`, :func:`.float`, :func:`.float64`
"""

tfloat = tfloat64
"""Alias for :py:data:`.tfloat64`."""

tstr = _tstr()
"""Hail type for text strings.

In Python, these are represented as strings.

See Also
--------
:class:`.StringExpression`, :func:`.str`
"""

tbool = _tbool()
"""Hail type for Boolean (``True`` or ``False``) values.

In Python, these are represented as :obj:`bool`.

See Also
--------
:class:`.BooleanExpression`, :func:`.bool`
"""

tcall = _tcall()
"""Hail type for a diploid genotype.

In Python, these are represented by :class:`.Call`.

See Also
--------
:class:`.CallExpression`, :class:`.Call`, :func:`.call`, :func:`.parse_call`,
:func:`.unphased_diploid_gt_index_call`
"""

hts_entry_schema = tstruct(GT=tcall, AD=tarray(tint32), DP=tint32, GQ=tint32, PL=tarray(tint32))

_numeric_types = {_tbool, _tint32, _tint64, _tfloat32, _tfloat64}
_primitive_types = _numeric_types.union({_tstr})
_interned_types = _primitive_types.union({_tcall})


@typecheck(t=HailType)
def is_numeric(t) -> bool:
    return t.__class__ in _numeric_types


@typecheck(t=HailType)
def is_primitive(t) -> bool:
    return t.__class__ in _primitive_types


@typecheck(t=HailType)
def is_container(t) -> bool:
    return (isinstance(t, tarray)
            or isinstance(t, tset)
            or isinstance(t, tdict))


@typecheck(t=HailType)
def is_compound(t) -> bool:
    return (is_container(t)
            or isinstance(t, tstruct)
            or isinstance(t, tunion)
            or isinstance(t, ttuple)
            or isinstance(t, tndarray))


def types_match(left, right) -> bool:
    return (len(left) == len(right)
            and all(map(lambda lr: lr[0].dtype == lr[1].dtype, zip(left, right))))

def from_numpy(np_dtype):
    if np_dtype == np.int32:
        return tint32
    elif np_dtype == np.int64:
        return tint64
    elif np_dtype == np.float32:
        return tfloat32
    elif np_dtype == np.float64:
        return tfloat64
    elif np_dtype == np.bool:
        return tbool
    else:
        raise ValueError(f"numpy type {np_dtype} could not be converted to a hail type.")


class tvariable(HailType):
    _cond_map = {
        'numeric': is_numeric,
        'int32': lambda x: x == tint32,
        'int64': lambda x: x == tint64,
        'float32': lambda x: x == tfloat32,
        'float64': lambda x: x == tfloat64,
        'locus': lambda x: isinstance(x, tlocus),
        'struct': lambda x: isinstance(x, tstruct),
        'union': lambda x: isinstance(x, tunion),
        'tuple': lambda x: isinstance(x, ttuple)
    }

    def __init__(self, name, cond):
        self.name = name
        self.cond = cond
        self.condf = tvariable._cond_map[cond] if cond else None
        self.box = Box.from_name(name)

    def unify(self, t):
        if self.condf and not self.condf(t):
            return False
        return self.box.unify(t)

    def clear(self):
        self.box.clear()

    def subst(self):
        return self.box.get()

    def __str__(self):
        s = '?' + self.name
        if self.cond:
            s = s + ':' + self.cond
        return s


import pprint

_old_printer = pprint.PrettyPrinter


class TypePrettyPrinter(pprint.PrettyPrinter):
    def _format(self, object, stream, indent, allowance, context, level):
        if isinstance(object, HailType):
            stream.write(object.pretty(self._indent_per_level))
        else:
            return _old_printer._format(self, object, stream, indent, allowance, context, level)


pprint.PrettyPrinter = TypePrettyPrinter  # monkey-patch pprint
