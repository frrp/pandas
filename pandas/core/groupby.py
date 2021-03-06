from itertools import izip
import types
import numpy as np

from pandas.core.frame import DataFrame
from pandas.core.generic import NDFrame
from pandas.core.index import Index, MultiIndex, _ensure_index
from pandas.core.internals import BlockManager, make_block
from pandas.core.series import Series
from pandas.core.panel import Panel
from pandas.util.decorators import cache_readonly, Appender
import pandas.core.algorithms as algos
import pandas.core.common as com
import pandas._tseries as lib


class GroupByError(Exception):
    pass


def _groupby_function(name, alias, npfunc):
    def f(self):
        try:
            return self._cython_agg_general(alias)
        except Exception:
            return self.aggregate(lambda x: npfunc(x, axis=self.axis))

    f.__doc__ = "Compute %s of group values" % name
    f.__name__ = name

    return f

def _first_compat(x, axis=0):
    x = np.asarray(x)
    x = x[com.notnull(x)]
    if len(x) == 0:
        return np.nan
    return x[0]

def _last_compat(x, axis=0):
    x = np.asarray(x)
    x = x[com.notnull(x)]
    if len(x) == 0:
        return np.nan
    return x[-1]


class GroupBy(object):
    """
    Class for grouping and aggregating relational data. See aggregate,
    transform, and apply functions on this object.

    It's easiest to use obj.groupby(...) to use GroupBy, but you can also do:

    ::

        grouped = groupby(obj, ...)

    Parameters
    ----------
    obj : pandas object
    axis : int, default 0
    level : int, default None
        Level of MultiIndex
    groupings : list of Grouping objects
        Most users should ignore this
    exclusions : array-like, optional
        List of columns to exclude
    name : string
        Most users should ignore this

    Notes
    -----
    After grouping, see aggregate, apply, and transform functions. Here are
    some other brief notes about usage. When grouping by multiple groups, the
    result index will be a MultiIndex (hierarhical) by default.

    Iteration produces (key, group) tuples, i.e. chunking the data by group. So
    you can write code like:

    ::

        grouped = obj.groupby(keys, axis=axis)
        for key, group in grouped:
            # do something with the data

    Function calls on GroupBy, if not specially implemented, "dispatch" to the
    grouped data. So if you group a DataFrame and wish to invoke the std()
    method on each group, you can simply do:

    ::

        df.groupby(mapper).std()

    rather than

    ::

        df.groupby(mapper).aggregate(np.std)

    You can pass arguments to these "wrapped" functions, too.

    See the online documentation for full exposition on these topics and much
    more

    Returns
    -------
    **Attributes**
    groups : dict
        {group name -> group labels}
    len(grouped) : int
        Number of groups
    """

    def __init__(self, obj, keys=None, axis=0, level=None,
                 grouper=None, exclusions=None, selection=None, as_index=True,
                 sort=True, group_keys=True):
        self._selection = selection

        if isinstance(obj, NDFrame):
            obj._consolidate_inplace()

        self.obj = obj
        self.axis = axis
        self.level = level

        if not as_index:
            if not isinstance(obj, DataFrame):
                raise TypeError('as_index=False only valid with DataFrame')
            if axis != 0:
                raise ValueError('as_index=False only valid for axis=0')

        self.as_index = as_index
        self.keys = keys
        self.sort = sort
        self.group_keys = group_keys

        if grouper is None:
            grouper, exclusions = _get_grouper(obj, keys, axis=axis,
                                               level=level, sort=sort)

        self.grouper = grouper
        self.exclusions = set(exclusions) if exclusions else set()

    def __len__(self):
        return len(self.indices)

    @property
    def groups(self):
        return self.grouper.groups

    @property
    def ngroups(self):
        return self.grouper.ngroups

    @property
    def indices(self):
        return self.grouper.indices

    @property
    def name(self):
        if self._selection is None:
            return None # 'result'
        else:
            return self._selection

    @property
    def _selection_list(self):
        if not isinstance(self._selection, (list, tuple, np.ndarray)):
            return [self._selection]
        return self._selection

    @property
    def _obj_with_exclusions(self):
        return self.obj

    def __getattr__(self, attr):
        if attr in self.obj:
            return self[attr]

        if hasattr(self.obj, attr) and attr != '_cache':
            return self._make_wrapper(attr)

        raise AttributeError("'%s' object has no attribute '%s'" %
                             (type(self).__name__, attr))

    def __getitem__(self, key):
        raise NotImplementedError

    def _make_wrapper(self, name):
        f = getattr(self.obj, name)
        if not isinstance(f, types.MethodType):
            return self.apply(lambda self: getattr(self, name))

        f = getattr(type(self.obj), name)

        def wrapper(*args, **kwargs):
            # a little trickery for aggregation functions that need an axis
            # argument
            kwargs_with_axis = kwargs.copy()
            if 'axis' not in kwargs_with_axis:
                kwargs_with_axis['axis'] = self.axis

            def curried_with_axis(x):
                return f(x, *args, **kwargs_with_axis)
            def curried(x):
                return f(x, *args, **kwargs)

            try:
                return self.apply(curried_with_axis)
            except Exception:
                return self.apply(curried)

        return wrapper

    def get_group(self, name, obj=None):
        if obj is None:
            obj = self.obj

        inds = self.indices[name]
        return obj.take(inds, axis=self.axis)

    def __iter__(self):
        """
        Groupby iterator

        Returns
        -------
        Generator yielding sequence of (name, subsetted object)
        for each group
        """
        return self.grouper.get_iterator(self.obj, axis=self.axis)

    def apply(self, func, *args, **kwargs):
        """
        Apply function and combine results together in an intelligent way. The
        split-apply-combine combination rules attempt to be as common sense
        based as possible. For example:

        case 1:
        group DataFrame
        apply aggregation function (f(chunk) -> Series)
        yield DataFrame, with group axis having group labels

        case 2:
        group DataFrame
        apply transform function ((f(chunk) -> DataFrame with same indexes)
        yield DataFrame with resulting chunks glued together

        case 3:
        group Series
        apply function with f(chunk) -> DataFrame
        yield DataFrame with result of chunks glued together

        Parameters
        ----------
        func : function

        Notes
        -----
        See online documentation for full exposition on how to use apply

        See also
        --------
        aggregate, transform

        Returns
        -------
        applied : type depending on grouped object and function
        """
        return self._python_apply_general(func, *args, **kwargs)

    def aggregate(self, func, *args, **kwargs):
        raise NotImplementedError

    def agg(self, func, *args, **kwargs):
        """
        See docstring for aggregate
        """
        return self.aggregate(func, *args, **kwargs)

    def _iterate_slices(self):
        yield self.name, self.obj

    def transform(self, func, *args, **kwargs):
        raise NotImplementedError

    def mean(self):
        """
        Compute mean of groups, excluding missing values

        For multiple groupings, the result index will be a MultiIndex
        """
        try:
            return self._cython_agg_general('mean')
        except GroupByError:
            raise
        except Exception:  # pragma: no cover
            f = lambda x: x.mean(axis=self.axis)
            return self._python_agg_general(f)

    def std(self, ddof=1):
        """
        Compute standard deviation of groups, excluding missing values

        For multiple groupings, the result index will be a MultiIndex
        """
        # todo, implement at cython level?
        if ddof == 1:
            return self._cython_agg_general('std')
        else:
            f = lambda x: x.std(ddof=ddof)
            return self._python_agg_general(f)

    def var(self, ddof=1):
        """
        Compute variance of groups, excluding missing values

        For multiple groupings, the result index will be a MultiIndex
        """
        if ddof == 1:
            return self._cython_agg_general('var')
        else:
            f = lambda x: x.var(ddof=ddof)
            return self._python_agg_general(f)

    def size(self):
        """
        Compute group sizes
        """
        return self.grouper.size()

    sum = _groupby_function('sum', 'add', np.sum)
    prod = _groupby_function('prod', 'prod', np.prod)
    min = _groupby_function('min', 'min', np.min)
    max = _groupby_function('max', 'max', np.max)
    first = _groupby_function('first', 'first', _first_compat)
    last = _groupby_function('last', 'last', _last_compat)

    def ohlc(self):
        """
        Compute sum of values, excluding missing values

        For multiple groupings, the result index will be a MultiIndex
        """
        return self._cython_agg_general('ohlc')

    # def last(self):
    #     return self.nth(-1)

    # def first(self):
    #     return self.nth(0)

    def nth(self, n):
        def picker(arr):
            if arr is not None:
                n_ok_pos = n >= 0 and len(arr) > n
                n_ok_neg = n < 0 and len(arr) >= n
                if n_ok_pos or n_ok_neg:
                    return arr.iget(n)
            return np.nan
        return self.agg(picker)

    def _cython_agg_general(self, how):
        output = {}
        for name, obj in self._iterate_slices():
            if not issubclass(obj.dtype.type, (np.number, np.bool_)):
                continue

            result, names = self.grouper.aggregate(obj.values, how)
            output[name] = result

        if len(output) == 0:
            raise GroupByError('No numeric types to aggregate')

        return self._wrap_aggregated_output(output, names)

    def _python_agg_general(self, func, *args, **kwargs):
        func = _intercept_function(func)
        agg_func = lambda x: func(x, *args, **kwargs)

        # iterate through "columns" ex exclusions to populate output dict
        output = {}
        for name, obj in self._iterate_slices():
            try:
                result, counts = self.grouper.agg_series(obj, agg_func)
                output[name] = result
            except TypeError:
                continue

        if len(output) == 0:
            return self._python_apply_general(func, *args, **kwargs)

        mask = counts.ravel() > 0
        for name, result in output.iteritems():
            output[name] = result[mask]

        return self._wrap_aggregated_output(output)

    def _python_apply_general(self, func, *args, **kwargs):
        func = _intercept_function(func)

        result_keys = []
        result_values = []

        not_indexed_same = False
        for key, group in self:
            object.__setattr__(group, 'name', key)

            # group might be modified
            group_axes = _get_axes(group)

            res = func(group, *args, **kwargs)

            if not _is_indexed_like(res, group_axes):
                not_indexed_same = True

            result_keys.append(key)
            result_values.append(res)

        return self._wrap_applied_output(result_keys, result_values,
                                         not_indexed_same=not_indexed_same)

    def _wrap_applied_output(self, *args, **kwargs):
        raise NotImplementedError

    def _concat_objects(self, keys, values, not_indexed_same=False):
        from pandas.tools.merge import concat

        if not not_indexed_same:
            result = concat(values, axis=self.axis)
            ax = self.obj._get_axis(self.axis)

            if isinstance(result, Series):
                result = result.reindex(ax)
            else:
                result = result.reindex_axis(ax, axis=self.axis)
        elif self.group_keys:
            group_keys = keys
            group_levels = self.grouper.levels
            group_names = self.grouper.names
            result = concat(values, axis=self.axis, keys=group_keys,
                            levels=group_levels, names=group_names)
        else:
            result = concat(values, axis=self.axis)

        return result

def _generate_groups(obj, group_index, ngroups, axis=0):
    if isinstance(obj, NDFrame) and not isinstance(obj, DataFrame):
        factory = obj._constructor
        obj = obj._data
    else:
        factory = None

    return generate_groups(obj, group_index, ngroups,
                           axis=axis, factory=factory)

@Appender(GroupBy.__doc__)
def groupby(obj, by, **kwds):
    if isinstance(obj, Series):
        klass = SeriesGroupBy
    elif isinstance(obj, DataFrame):
        klass = DataFrameGroupBy
    else: # pragma: no cover
        raise TypeError('invalid type: %s' % type(obj))

    return klass(obj, by, **kwds)

def _get_axes(group):
    if isinstance(group, Series):
        return [group.index]
    else:
        return group.axes

def _is_indexed_like(obj, axes):
    if isinstance(obj, Series):
        if len(axes) > 1:
            return False
        return obj.index.equals(axes[0])
    elif isinstance(obj, DataFrame):
        return obj.index.equals(axes[0])

    return False

class Grouper(object):
    """

    """
    def __init__(self, axis, groupings, sort=True, group_keys=True):
        self.axis = axis
        self.groupings = groupings
        self.sort = sort
        self.group_keys = group_keys

    @property
    def shape(self):
        return tuple(ping.ngroups for ping in self.groupings)

    def __iter__(self):
        return iter(self.indices)

    @property
    def nkeys(self):
        return len(self.groupings)

    def get_iterator(self, data, axis=0):
        """
        Groupby iterator

        Returns
        -------
        Generator yielding sequence of (name, subsetted object)
        for each group
        """
        if len(self.groupings) == 1:
            indices = self.indices
            groups = indices.keys()
            try:
                groups = sorted(groups)
            except Exception: # pragma: no cover
                pass

            for name in groups:
                inds = indices[name]
                group = data.take(inds, axis=axis)
                yield name, group
        else:
            # provide "flattened" iterator for multi-group setting
            comp_ids, _, ngroups = self.group_info
            label_list = self.labels
            level_list = self.levels
            mapper = _KeyMapper(comp_ids, ngroups, label_list, level_list)

            for label, group in _generate_groups(data, comp_ids, ngroups,
                                                 axis=axis):
                key = mapper.get_key(label)
                yield key, group

    @cache_readonly
    def indices(self):
        if len(self.groupings) == 1:
            return self.groupings[0].indices
        else:
            label_list = [ping.labels for ping in self.groupings]
            keys = [ping.group_index for ping in self.groupings]
            return _get_indices_dict(label_list, keys)

    @property
    def labels(self):
        return [ping.labels for ping in self.groupings]

    @property
    def levels(self):
        return [ping.group_index for ping in self.groupings]

    @property
    def names(self):
        return [ping.name for ping in self.groupings]

    def size(self):
        """
        Compute group sizes
        """
        # TODO: better impl
        labels, _, ngroups = self.group_info
        bin_counts = Series(labels).value_counts()
        bin_counts = bin_counts.reindex(np.arange(ngroups))
        bin_counts.index = self.result_index
        return bin_counts

    @cache_readonly
    def groups(self):
        if len(self.groupings) == 1:
            return self.groupings[0].groups
        else:
            to_groupby = zip(*(ping.grouper for ping in self.groupings))
            to_groupby = Index(to_groupby)

            return self.axis.groupby(to_groupby)

    @cache_readonly
    def group_info(self):
        comp_ids, obs_group_ids = self._get_compressed_labels()

        ngroups = len(obs_group_ids)
        comp_ids = com._ensure_int64(comp_ids)
        return comp_ids, obs_group_ids, ngroups

    def _get_compressed_labels(self):
        all_labels = [ping.labels for ping in self.groupings]
        if self._overflow_possible:
            tups = lib.fast_zip(all_labels)
            labs, uniques, _ = algos.factorize(tups)

            if self.sort:
                uniques, labs = _reorder_by_uniques(uniques, labs)

            return labs, uniques
        else:
            if len(all_labels) > 1:
                group_index = get_group_index(all_labels, self.shape)
            else:
                group_index = all_labels[0]
            comp_ids, obs_group_ids = _compress_group_index(group_index)
            return comp_ids, obs_group_ids

    @cache_readonly
    def _overflow_possible(self):
        return _int64_overflow_possible(self.shape)

    @cache_readonly
    def ngroups(self):
        return len(self.result_index)

    @cache_readonly
    def result_index(self):
        recons = self.get_group_levels()
        return MultiIndex.from_arrays(recons, names=self.names)

    def get_group_levels(self):
        obs_ids = self.group_info[1]
        if self._overflow_possible:
            recons_labels = [np.array(x) for x in izip(*obs_ids)]
        else:
            recons_labels = decons_group_index(obs_ids, self.shape)

        name_list = []
        for ping, labels in zip(self.groupings, recons_labels):
            labels = com._ensure_platform_int(labels)
            name_list.append(ping.group_index.take(labels))

        return name_list

    #------------------------------------------------------------
    # Aggregation functions

    _cython_functions = {
        'add' : lib.group_add,
        'prod' : lib.group_prod,
        'min' : lib.group_min,
        'max' : lib.group_max,
        'mean' : lib.group_mean,
        'var' : lib.group_var,
        'std' : lib.group_var,
        'first': lambda a, b, c, d: lib.group_nth(a, b, c, d, 1),
        'last': lib.group_last
    }

    _cython_transforms = {
        'std' : np.sqrt
    }

    _cython_arity = {
        'ohlc' : 4, # OHLC
    }

    _name_functions = {}

    _filter_empty_groups = True

    def aggregate(self, values, how, axis=0):
        values = com._ensure_float64(values)
        arity = self._cython_arity.get(how, 1)

        vdim = values.ndim
        if vdim == 1:
            values = values[:, None]
            out_shape = (self.ngroups, arity)
        else:
            if axis > 0:
                values = values.swapaxes(0, axis)
            if arity > 1:
                raise NotImplementedError
            out_shape = (self.ngroups,) + values.shape[1:]

        # will be filled in Cython function
        result = np.empty(out_shape, dtype=np.float64)
        counts = np.zeros(self.ngroups, dtype=np.int64)

        result = self._aggregate(result, counts, values, how)

        if self._filter_empty_groups:
            result = lib.row_bool_subset(result,
                                         (counts > 0).view(np.uint8))

        if vdim == 1 and arity == 1:
            result = result[:, 0]

        if how in self._name_functions:
            # TODO
            names = self._name_functions[how]()
        else:
            names = None

        if axis > 0:
            result = result.swapaxes(0, axis)

        return result, names

    def _aggregate(self, result, counts, values, how):
        agg_func = self._cython_functions[how]
        trans_func = self._cython_transforms.get(how, lambda x: x)

        comp_ids, _, ngroups = self.group_info
        if values.ndim > 3:
            # punting for now
            raise NotImplementedError
        elif values.ndim > 2:
            for i, chunk in enumerate(values.transpose(2, 0, 1)):
                agg_func(result[:, :, i], counts, chunk, comp_ids)
        else:
            agg_func(result, counts, values, comp_ids)

        return trans_func(result)

    def agg_series(self, obj, func):
        try:
            return self._aggregate_series_fast(obj, func)
        except Exception:
            return self._aggregate_series_pure_python(obj, func)

    def _aggregate_series_fast(self, obj, func):
        func = _intercept_function(func)

        if obj.index._has_complex_internals:
            raise TypeError('Incompatible index for Cython grouper')

        group_index, _, ngroups = self.group_info

        # avoids object / Series creation overhead
        dummy = obj[:0].copy()
        indexer = lib.groupsort_indexer(group_index, ngroups)[0]
        obj = obj.take(indexer)
        group_index = com.ndtake(group_index, indexer)
        grouper = lib.SeriesGrouper(obj, func, group_index, ngroups,
                                    dummy)
        result, counts = grouper.get_result()
        return result, counts

    def _aggregate_series_pure_python(self, obj, func):
        group_index, _, ngroups = self.group_info

        counts = np.zeros(ngroups, dtype=int)
        result = None

        group_index, _, ngroups = self.group_info

        for label, group in _generate_groups(obj, group_index, ngroups,
                                             axis=self.axis):
            res = func(group)
            if result is None:
                try:
                    assert(not isinstance(res, np.ndarray))
                    assert(not isinstance(res, list))
                    result = np.empty(ngroups, dtype='O')
                except Exception:
                    raise ValueError('function does not reduce')

            counts[label] = group.shape[0]
            result[label] = res

        result = lib.maybe_convert_objects(result, try_float=0)
        return result, counts


def generate_bins_generic(values, binner, closed):
    """
    Generate bin edge offsets and bin labels for one array using another array
    which has bin edge values. Both arrays must be sorted.

    Parameters
    ----------
    values : array of values
    binner : a comparable array of values representing bins into which to bin
        the first array. Note, 'values' end-points must fall within 'binner'
        end-points.
    closed : which end of bin is closed; left (default), right

    Returns
    -------
    bins : array of offsets (into 'values' argument) of bins.
        Zero and last edge are excluded in result, so for instance the first
        bin is values[0:bin[0]] and the last is values[bin[-1]:]
    """
    lenidx = len(values)
    lenbin = len(binner)

    if lenidx <= 0 or lenbin <= 0:
        raise ValueError("Invalid length for values or for binner")

    # check binner fits data
    if values[0] < binner[0]:
        raise ValueError("Values falls before first bin")

    if values[lenidx-1] > binner[lenbin-1]:
        raise ValueError("Values falls after last bin")

    bins   = np.empty(lenbin - 1, dtype=np.int64)

    j  = 0 # index into values
    bc = 0 # bin count

    # linear scan, presume nothing about values/binner except that it
    # fits ok
    for i in range(0, lenbin-1):
        r_bin = binner[i+1]

        # count values in current bin, advance to next bin
        while j < lenidx and (values[j] < r_bin or
                              (closed == 'right' and values[j] == r_bin)):
            j += 1

        bins[bc] = j
        bc += 1

    return bins


class CustomGrouper(object):

    def get_grouper(self, obj):
        raise NotImplementedError


class BinGrouper(Grouper):

    def __init__(self, bins, binlabels, filter_empty=False):
        self.bins = com._ensure_int64(bins)
        self.binlabels = _ensure_index(binlabels)
        self._filter_empty_groups = filter_empty

    @property
    def nkeys(self):
        return 1

    def get_iterator(self, data, axis=0):
        """
        Groupby iterator

        Returns
        -------
        Generator yielding sequence of (name, subsetted object)
        for each group
        """
        if axis == 1:
            raise NotImplementedError

        start = 0
        for edge, label in zip(self.bins, self.binlabels):
            yield label, data[start:edge]
            start = edge

        if edge < len(data):
            yield self.binlabels[-1], data[edge:]

    @cache_readonly
    def ngroups(self):
        return len(self.binlabels)

    @cache_readonly
    def result_index(self):
        return self.binlabels

    @property
    def levels(self):
        return [self.binlabels]

    @property
    def names(self):
        return [self.binlabels.name]

    #----------------------------------------------------------------------
    # cython aggregation

    _cython_functions = {
        'add' : lib.group_add_bin,
        'prod' : lib.group_prod_bin,
        'mean' : lib.group_mean_bin,
        'min' : lib.group_min_bin,
        'max' : lib.group_max_bin,
        'var' : lib.group_var_bin,
        'std' : lib.group_var_bin,
        'ohlc' : lib.group_ohlc,
        'first': lambda a, b, c, d: lib.group_nth_bin(a, b, c, d, 1),
        'last': lib.group_last_bin
    }

    _name_functions = {
        'ohlc' : lambda *args: ['open', 'high', 'low', 'close']
    }

    _filter_empty_groups = True

    def _aggregate(self, result, counts, values, how):
        agg_func = self._cython_functions[how]
        trans_func = self._cython_transforms.get(how, lambda x: x)

        if values.ndim > 3:
            # punting for now
            raise NotImplementedError
        elif values.ndim > 2:
            for i, chunk in enumerate(values.transpose(2, 0, 1)):
                agg_func(result[:, :, i], counts, chunk, self.bins)
        else:
            agg_func(result, counts, values, self.bins)

        return trans_func(result)

    def agg_series(self, obj, func):
        dummy = obj[:0]
        grouper = lib.SeriesBinGrouper(obj, func, self.bins, dummy)
        return grouper.get_result()


class Grouping(object):
    """
    Holds the grouping information for a single key

    Parameters
    ----------
    index : Index
    grouper :
    name :
    level :

    Returns
    -------
    **Attributes**:
      * indices : dict of {group -> index_list}
      * labels : ndarray, group labels
      * ids : mapping of label -> group
      * counts : array of group counts
      * group_index : unique groups
      * groups : dict of {group -> label_list}
    """
    def __init__(self, index, grouper=None, name=None, level=None,
                 sort=True):

        self.name = name
        self.level = level
        self.grouper = _convert_grouper(index, grouper)
        self.index = index
        self.sort = sort

        # right place for this?
        if isinstance(grouper, Series) and name is None:
            self.name = grouper.name

        # pre-computed
        self._was_factor = False

        # did we pass a custom grouper object? Do nothing
        if isinstance(grouper, Grouper):
            return

        if level is not None:
            if not isinstance(level, int):
                assert(level in index.names)
                level = index.names.index(level)

            inds = index.labels[level]
            level_index = index.levels[level]

            if self.name is None:
                self.name = index.names[level]

            # XXX complete hack

            level_values = index.levels[level].take(inds)
            if grouper is not None:
                self.grouper = level_values.map(self.grouper)
            else:
                self._was_factor = True
                self._labels = inds
                self._group_index = level_index
                self.grouper = level_values
        else:
            if isinstance(self.grouper, (list, tuple)):
                self.grouper = com._asarray_tuplesafe(self.grouper)

            # no level passed
            if not isinstance(self.grouper, np.ndarray):
                self.grouper = self.index.map(self.grouper)

    def __repr__(self):
        return 'Grouping(%s)' % self.name

    def __iter__(self):
        return iter(self.indices)

    _labels = None
    _counts = None
    _group_index = None

    @property
    def ngroups(self):
        return len(self.group_index)

    @cache_readonly
    def indices(self):
        return _groupby_indices(self.grouper)

    @property
    def labels(self):
        if self._labels is None:
            self._make_labels()
        return self._labels

    @property
    def counts(self):
        if self._counts is None:
            if self._was_factor:
                self._counts = lib.group_count(com._ensure_int64(self.labels),
                                               self.ngroups)
            else:
                self._make_labels()
        return self._counts

    @property
    def group_index(self):
        if self._group_index is None:
            self._make_labels()
        return self._group_index

    def _make_labels(self):
        if self._was_factor:  # pragma: no cover
            raise Exception('Should not call this method grouping by level')
        else:
            labs, uniques, counts = algos.factorize(self.grouper,
                                                    sort=self.sort)
            uniques = Index(uniques, name=self.name)
            self._labels = labs
            self._group_index = uniques
            self._counts = counts

    _groups = None
    @property
    def groups(self):
        if self._groups is None:
            self._groups = self.index.groupby(self.grouper)
        return self._groups


def _get_grouper(obj, key=None, axis=0, level=None, sort=True):
    group_axis = obj._get_axis(axis)

    if level is not None:
        if not isinstance(group_axis, MultiIndex):
            if level > 0:
                raise ValueError('level > 0 only valid with MultiIndex')
            else:
                level = None
                key = group_axis

    if isinstance(key, CustomGrouper):
        gpr = key.get_grouper(obj)
        return gpr, []
    elif isinstance(key, Grouper):
        return key, []

    if not isinstance(key, (tuple, list)):
        keys = [key]
    else:
        keys = key

    # what are we after, exactly?
    match_axis_length = len(keys) == len(group_axis)
    any_callable = any(callable(g) or isinstance(g, dict) for g in keys)
    any_arraylike = any(isinstance(g, (list, tuple, np.ndarray))
                        for g in keys)

    try:
        if isinstance(obj, DataFrame):
            all_in_columns = all(g in obj.columns for g in keys)
        else:
            all_in_columns = False
    except Exception:
        all_in_columns = False

    if (not any_callable and not all_in_columns
        and not any_arraylike and match_axis_length
        and not level):
        keys = [com._asarray_tuplesafe(keys)]

    if isinstance(level, (tuple, list)):
        if key is None:
            keys = [None] * len(level)
        levels = level
    else:
        levels = [level] * len(keys)

    groupings = []
    exclusions = []
    for i, (gpr, level) in enumerate(zip(keys, levels)):
        name = None
        if _is_label_like(gpr):
            exclusions.append(gpr)
            name = gpr
            gpr = obj[gpr]
        ping = Grouping(group_axis, gpr, name=name, level=level, sort=sort)
        if ping.name is None:
            ping.name = 'key_%d' % i
        groupings.append(ping)

    if len(groupings) == 0:
        raise ValueError('No group keys passed!')

    grouper = Grouper(group_axis, groupings, sort=sort)

    return grouper, exclusions

def _is_label_like(val):
    return isinstance(val, basestring) or np.isscalar(val)

def _convert_grouper(axis, grouper):
    if isinstance(grouper, dict):
        return grouper.get
    elif isinstance(grouper, Series):
        if grouper.index.equals(axis):
            return grouper.values
        else:
            return grouper.reindex(axis).values
    elif isinstance(grouper, (list, np.ndarray)):
        assert(len(grouper) == len(axis))
        return grouper
    else:
        return grouper

class SeriesGroupBy(GroupBy):

    def aggregate(self, func_or_funcs, *args, **kwargs):
        """
        Apply aggregation function or functions to groups, yielding most likely
        Series but in some cases DataFrame depending on the output of the
        aggregation function

        Parameters
        ----------
        func_or_funcs : function or list / dict of functions
            List/dict of functions will produce DataFrame with column names
            determined by the function names themselves (list) or the keys in
            the dict

        Notes
        -----
        agg is an alias for aggregate. Use it.

        Example
        -------
        >>> series
        bar    1.0
        baz    2.0
        qot    3.0
        qux    4.0

        >>> mapper = lambda x: x[0] # first letter
        >>> grouped = series.groupby(mapper)

        >>> grouped.aggregate(np.sum)
        b    3.0
        q    7.0

        >>> grouped.aggregate([np.sum, np.mean, np.std])
           mean  std  sum
        b  1.5   0.5  3
        q  3.5   0.5  7

        >>> grouped.agg({'result' : lambda x: x.mean() / x.std(),
        ...              'total' : np.sum})
           result  total
        b  2.121   3
        q  4.95    7

        See also
        --------
        apply, transform

        Returns
        -------
        Series or DataFrame
        """
        if isinstance(func_or_funcs, basestring):
            return getattr(self, func_or_funcs)(*args, **kwargs)

        if hasattr(func_or_funcs,'__iter__'):
            ret = self._aggregate_multiple_funcs(func_or_funcs)
        else:
            cyfunc = _intercept_cython(func_or_funcs)
            if cyfunc and not args and not kwargs:
                return getattr(self, cyfunc)()

            if self.grouper.nkeys > 1:
                return self._python_agg_general(func_or_funcs, *args, **kwargs)

            try:
                return self._python_agg_general(func_or_funcs, *args, **kwargs)
            except Exception:
                result = self._aggregate_named(func_or_funcs, *args, **kwargs)

            index = Index(sorted(result), name=self.grouper.names[0])
            ret = Series(result, index=index)

        if not self.as_index:  # pragma: no cover
            print 'Warning, ignoring as_index=True'

        return ret

    def _aggregate_multiple_funcs(self, arg):
        if isinstance(arg, dict):
            columns = arg.keys()
            arg = arg.items()
        elif isinstance(arg[0], (tuple, list)):
            # indicated column order
            columns = list(zip(*arg))[0]
        else:
            # list of functions / function names
            columns = []
            for f in arg:
                if isinstance(f, basestring):
                    columns.append(f)
                else:
                    columns.append(f.__name__)
            arg = zip(columns, arg)

        results = {}

        for name, func in arg:
            results[name] = self.aggregate(func)

        return DataFrame(results, columns=columns)

    def _wrap_aggregated_output(self, output, names=None):
        # sort of a kludge
        output = output[self.name]
        index = self.grouper.result_index

        if names is not None:
            return DataFrame(output, index=index, columns=names)
        else:
            return Series(output, index=index, name=self.name)

    def _wrap_applied_output(self, keys, values, not_indexed_same=False):
        if len(keys) == 0:
            return Series([])

        def _get_index():
            if self.grouper.nkeys > 1:
                index = MultiIndex.from_tuples(keys, names=self.grouper.names)
            else:
                index = Index(keys, name=self.grouper.names[0])
            return index

        if isinstance(values[0], dict):
            # # GH #823
            return DataFrame(values, index=keys).stack()

        if isinstance(values[0], (Series, dict)):
            return self._concat_objects(keys, values,
                                        not_indexed_same=not_indexed_same)
        elif isinstance(values[0], DataFrame):
            # possible that Series -> DataFrame by applied function
            return self._concat_objects(keys, values,
                                        not_indexed_same=not_indexed_same)
        else:
            return Series(values, index=_get_index())

    def _aggregate_named(self, func, *args, **kwargs):
        result = {}

        for name, group in self:
            group.name = name
            output = func(group, *args, **kwargs)
            if isinstance(output, np.ndarray):
                raise Exception('Must produce aggregated value')
            result[name] = output

        return result

    def transform(self, func, *args, **kwargs):
        """
        Call function producing a like-indexed Series on each group and return
        a Series with the transformed values

        Parameters
        ----------
        func : function
            To apply to each group. Should return a Series with the same index

        Example
        -------
        >>> grouped.transform(lambda x: (x - x.mean()) / x.std())

        Returns
        -------
        transformed : Series
        """
        result = self.obj.copy()

        for name, group in self:
            object.__setattr__(group, 'name', name)
            res = func(group, *args, **kwargs)
            indexer = self.obj.index.get_indexer(group.index)
            np.put(result, indexer, res)

        return result

class NDFrameGroupBy(GroupBy):

    def _iterate_slices(self):
        if self.axis == 0:
            # kludge
            if self._selection is None:
                slice_axis = self.obj.columns
            else:
                slice_axis = self._selection_list
            slicer = lambda x: self.obj[x]
        else:
            slice_axis = self.obj.index
            slicer = self.obj.xs

        for val in slice_axis:
            if val in self.exclusions:
                continue

            yield val, slicer(val)

    def _cython_agg_general(self, how):
        new_blocks = self._cython_agg_blocks(how)
        return self._wrap_agged_blocks(new_blocks)

    def _wrap_agged_blocks(self, blocks):
        obj = self._obj_with_exclusions

        new_axes = list(obj._data.axes)
        new_axes[self.axis] = self.grouper.result_index
        mgr = BlockManager(blocks, new_axes)

        new_obj = type(obj)(mgr)

        return self._post_process_cython_aggregate(new_obj)

    _block_agg_axis = 0

    def _cython_agg_blocks(self, how):
        data, agg_axis = self._get_data_to_aggregate()

        new_blocks = []

        for block in data.blocks:
            values = block.values
            if not issubclass(values.dtype.type, (np.number, np.bool_)):
                continue

            values = com._ensure_float64(values)
            result, names = self.grouper.aggregate(values, how, axis=agg_axis)
            newb = make_block(result, block.items, block.ref_items)
            new_blocks.append(newb)

        if len(new_blocks) == 0:
            raise GroupByError('No numeric types to aggregate')

        return new_blocks

    def _get_data_to_aggregate(self):
        obj = self._obj_with_exclusions
        if self.axis == 0:
            return obj.swapaxes(0, 1)._data, 1
        else:
            return obj._data, self.axis

    def _post_process_cython_aggregate(self, obj):
        # undoing kludge from below
        if self.axis == 0:
            obj = obj.swapaxes(0, 1)
        return obj

    @cache_readonly
    def _obj_with_exclusions(self):
        if self._selection is not None:
            return self.obj.reindex(columns=self._selection_list)

        if len(self.exclusions) > 0:
            return self.obj.drop(self.exclusions, axis=1)
        else:
            return self.obj

    def aggregate(self, arg, *args, **kwargs):
        """
        Aggregate using input function or dict of {column -> function}

        Parameters
        ----------
        arg : function or dict
            Function to use for aggregating groups. If a function, must either
            work when passed a DataFrame or when passed to DataFrame.apply. If
            pass a dict, the keys must be DataFrame column names

        Returns
        -------
        aggregated : DataFrame
        """
        if isinstance(arg, basestring):
            return getattr(self, arg)(*args, **kwargs)

        result = {}
        if isinstance(arg, dict):
            if self.axis != 0:  # pragma: no cover
                raise ValueError('Can only pass dict with axis=0')

            obj = self._obj_with_exclusions

            if any(isinstance(x, (list, tuple, dict)) for x in arg.values()):
                new_arg = {}
                for k, v in arg.iteritems():
                    if not isinstance(v, (tuple, list, dict)):
                        new_arg[k] = [v]
                    else:
                        new_arg[k] = v
                arg = new_arg

            keys = []
            if self._selection is not None:
                subset = obj[self._selection]
                if isinstance(subset, DataFrame):
                    raise NotImplementedError

                for fname, agg_how in arg.iteritems():
                    colg = SeriesGroupBy(subset, selection=self._selection,
                                         grouper=self.grouper)
                    result[fname] = colg.aggregate(agg_how)
                    keys.append(fname)
            else:
                for col, agg_how in arg.iteritems():
                    colg = SeriesGroupBy(obj[col], selection=col,
                                         grouper=self.grouper)
                    result[col] = colg.aggregate(agg_how)
                    keys.append(col)

            if isinstance(result.values()[0], DataFrame):
                from pandas.tools.merge import concat
                result = concat([result[k] for k in keys], keys=keys, axis=1)
            else:
                result = DataFrame(result)
        elif isinstance(arg, list):
            return self._aggregate_multiple_funcs(arg)
        else:
            cyfunc = _intercept_cython(arg)
            if cyfunc and not args and not kwargs:
                return getattr(self, cyfunc)()

            if self.grouper.nkeys > 1:
                return self._python_agg_general(arg, *args, **kwargs)
            else:
                result = self._aggregate_generic(arg, *args, **kwargs)

        if not self.as_index:
            if isinstance(result.index, MultiIndex):
                zipped = zip(result.index.levels, result.index.labels,
                             result.index.names)
                for i, (lev, lab, name) in enumerate(zipped):
                    result.insert(i, name, com.ndtake(lev.values, lab))
                result = result.consolidate()
            else:
                values = result.index.values
                name = self.grouper.groupings[0].name
                result.insert(0, name, values)
            result.index = np.arange(len(result))

        return result

    def _aggregate_multiple_funcs(self, arg):
        from pandas.tools.merge import concat

        if self.axis != 0:
            raise NotImplementedError

        obj = self._obj_with_exclusions

        results = []
        keys = []
        for col in obj:
            try:
                colg = SeriesGroupBy(obj[col], selection=col,
                                     grouper=self.grouper)
                results.append(colg.aggregate(arg))
                keys.append(col)
            except (TypeError, GroupByError):
                pass

        result = concat(results, keys=keys, axis=1)

        return result

    def _aggregate_generic(self, func, *args, **kwargs):
        assert(self.grouper.nkeys == 1)

        axis = self.axis
        obj = self._obj_with_exclusions

        result = {}
        if axis != obj._het_axis:
            try:
                for name in self.indices:
                    data = self.get_group(name, obj=obj)
                    result[name] = func(data, *args, **kwargs)
            except Exception:
                return self._aggregate_item_by_item(func, *args, **kwargs)
        else:
            for name in self.indices:
                try:
                    data = self.get_group(name, obj=obj)
                    result[name] = func(data, *args, **kwargs)
                except Exception:
                    wrapper = lambda x: func(x, *args, **kwargs)
                    result[name] = data.apply(wrapper, axis=axis)

        return self._wrap_generic_output(result, obj)

    def _wrap_aggregated_output(self, output, names=None):
        raise NotImplementedError

    def _aggregate_item_by_item(self, func, *args, **kwargs):
        # only for axis==0

        obj = self._obj_with_exclusions
        result = {}
        cannot_agg = []
        for item in obj:
            try:
                colg = SeriesGroupBy(obj[item], selection=item,
                                     grouper=self.grouper)
                result[item] = colg.aggregate(func, *args, **kwargs)
            except (ValueError, TypeError):
                cannot_agg.append(item)
                continue

        result_columns = obj.columns
        if cannot_agg:
            result_columns = result_columns.drop(cannot_agg)

        return DataFrame(result, columns=result_columns)

    def _decide_output_index(self, output, labels):
        if len(output) == len(labels):
            output_keys = labels
        else:
            output_keys = sorted(output)
            try:
                output_keys.sort()
            except Exception:  # pragma: no cover
                pass

            if isinstance(labels, MultiIndex):
                output_keys = MultiIndex.from_tuples(output_keys,
                                                     names=labels.names)

        return output_keys

    def _wrap_applied_output(self, keys, values, not_indexed_same=False):
        if len(keys) == 0:
            # XXX
            return DataFrame({})

        key_names = self.grouper.names

        if isinstance(values[0], DataFrame):
            return self._concat_objects(keys, values,
                                        not_indexed_same=not_indexed_same)
        else:
            if len(self.grouper.groupings) > 1:
                key_index = MultiIndex.from_tuples(keys, names=key_names)
            else:
                ping = self.grouper.groupings[0]
                if len(keys) == ping.ngroups:
                    key_index = ping.group_index
                    key_index.name = key_names[0]

                    key_lookup = Index(keys)
                    indexer = key_lookup.get_indexer(key_index)

                    # reorder the values
                    values = [values[i] for i in indexer]
                else:
                    key_index = Index(keys, name=key_names[0])

            if isinstance(values[0], np.ndarray):
                if self.axis == 0:
                    stacked_values = np.vstack([np.asarray(x)
                                                for x in values])
                    columns = values[0].index
                    index = key_index
                else:
                    stacked_values = np.vstack([np.asarray(x)
                                                for x in values]).T
                    index = values[0].index
                    columns = key_index
                return DataFrame(stacked_values, index=index,
                                 columns=columns)
            else:
                return Series(values, index=key_index)

    def transform(self, func, *args, **kwargs):
        """
        Call function producing a like-indexed DataFrame on each group and
        return a DataFrame having the same indexes as the original object
        filled with the transformed values

        Parameters
        ----------
        f : function
            Function to apply to each subframe

        Note
        ----
        Each subframe is endowed the attribute 'name' in case you need to know
        which group you are working on.

        Example
        --------
        >>> grouped = df.groupby(lambda x: mapping[x])
        >>> grouped.transform(lambda x: (x - x.mean()) / x.std())
        """
        from pandas.tools.merge import concat

        applied = []

        obj = self._obj_with_exclusions
        for name, group in self:
            object.__setattr__(group, 'name', name)

            try:
                wrapper = lambda x: func(x, *args, **kwargs)
                res = group.apply(wrapper, axis=self.axis)
            except Exception: # pragma: no cover
                res = func(group, *args, **kwargs)

            # broadcasting
            if isinstance(res, Series):
                if res.index is obj.index:
                    group.T.values[:] = res
                else:
                    group.values[:] = res

                applied.append(group)
            else:
                applied.append(res)

        concat_index = obj.columns if self.axis == 0 else obj.index
        concatenated = concat(applied, join_axes=[concat_index],
                              axis=self.axis, verify_integrity=False)
        return concatenated.reindex_like(obj)



class DataFrameGroupBy(NDFrameGroupBy):

    _block_agg_axis = 1

    def __getitem__(self, key):
        if self._selection is not None:
            raise Exception('Column(s) %s already selected' % self._selection)

        if isinstance(key, (list, tuple, np.ndarray)) or not self.as_index:
            return DataFrameGroupBy(self.obj, self.grouper, selection=key,
                                    grouper=self.grouper,
                                    exclusions=self.exclusions,
                                    as_index=self.as_index)
        else:
            if key not in self.obj:  # pragma: no cover
                raise KeyError(str(key))
            # kind of a kludge
            return SeriesGroupBy(self.obj[key], selection=key,
                                 grouper=self.grouper,
                                 exclusions=self.exclusions)

    def _wrap_generic_output(self, result, obj):
        result_index = self.grouper.levels[0]

        if result:
            if self.axis == 0:
                result = DataFrame(result, index=obj.columns,
                                   columns=result_index).T
            else:
                result = DataFrame(result, index=obj.index,
                                   columns=result_index)
        else:
            result = DataFrame(result)

        return result

    def _get_data_to_aggregate(self):
        obj = self._obj_with_exclusions
        if self.axis == 1:
            return obj.T._data, 1
        else:
            return obj._data, 1

    def _wrap_aggregated_output(self, output, names=None):
        agg_axis = 0 if self.axis == 1 else 1
        agg_labels = self._obj_with_exclusions._get_axis(agg_axis)

        output_keys = self._decide_output_index(output, agg_labels)

        if not self.as_index:
            result = DataFrame(output, columns=output_keys)
            group_levels = self.grouper.get_group_levels()
            zipped = zip(self.grouper.names, group_levels)

            for i, (name, labels) in enumerate(zipped):
                result.insert(i, name, labels)
            result = result.consolidate()
        else:
            index = self.grouper.result_index
            result = DataFrame(output, index=index, columns=output_keys)

        if self.axis == 1:
            result = result.T

        return result

    def _post_process_cython_aggregate(self, obj):
        # undoing kludge from below
        if self.axis == 0:
            obj = obj.T
        return obj

    def _wrap_agged_blocks(self, blocks):
        obj = self._obj_with_exclusions

        if self.axis == 0:
            agg_labels = obj.columns
        else:
            agg_labels = obj.index

        if sum(len(x.items) for x in blocks) == len(agg_labels):
            output_keys = agg_labels
        else:
            all_items = []
            for b in blocks:
                all_items.extend(b.items)
            output_keys = agg_labels[agg_labels.isin(all_items)]

            for blk in blocks:
                blk.set_ref_items(output_keys, maybe_rename=False)

        if not self.as_index:
            index = np.arange(blocks[0].values.shape[1])
            mgr = BlockManager(blocks, [output_keys, index])
            result = DataFrame(mgr)

            group_levels = self.grouper.get_group_levels()
            zipped = zip(self.grouper.names, group_levels)

            for i, (name, labels) in enumerate(zipped):
                result.insert(i, name, labels)
            result = result.consolidate()
        else:
            index = self.grouper.result_index
            mgr = BlockManager(blocks, [output_keys, index])
            result = DataFrame(mgr)

        if self.axis == 1:
            result = result.T

        return result

class PanelGroupBy(NDFrameGroupBy):

    def _iterate_slices(self):
        if self.axis == 0:
            # kludge
            if self._selection is None:
                slice_axis = self.obj.items
            else:
                slice_axis = self._selection_list
            slicer = lambda x: self.obj[x]
        else:
            raise NotImplementedError
            # slice_axis = self.obj.index
            # slicer = lambda x: self.obj.xs(x, axis=self.axis)

        for val in slice_axis:
            if val in self.exclusions:
                continue

            yield val, slicer(val)

    def aggregate(self, arg, *args, **kwargs):
        """
        Aggregate using input function or dict of {column -> function}

        Parameters
        ----------
        arg : function or dict
            Function to use for aggregating groups. If a function, must either
            work when passed a Panel or when passed to Panel.apply. If
            pass a dict, the keys must be DataFrame column names

        Returns
        -------
        aggregated : Panel
        """
        if isinstance(arg, basestring):
            return getattr(self, arg)(*args, **kwargs)

        return self._aggregate_generic(arg, *args, **kwargs)

    def _wrap_generic_output(self, result, obj):

        new_axes = list(obj.axes)
        new_axes[self.axis] = self.grouper.result_index

        result = Panel._from_axes(result, new_axes)

        if self.axis > 0:
            result = result.swapaxes(0, self.axis)

        return result

    def _aggregate_item_by_item(self, func, *args, **kwargs):
        obj = self._obj_with_exclusions
        result = {}
        cannot_agg = []

        if self.axis > 0:
            for item in obj:
                try:
                    itemg = DataFrameGroupBy(obj[item],
                                             axis=self.axis - 1,
                                             grouper=self.grouper)
                    result[item] = itemg.aggregate(func, *args, **kwargs)
                except (ValueError, TypeError):
                    raise
                    # cannot_agg.append(item)
                    # continue
            new_axes = list(obj.axes)
            new_axes[self.axis] = self.grouper.result_index
            return Panel._from_axes(result, new_axes)
        else:
            raise NotImplementedError

    def _wrap_aggregated_output(self, output, names=None):
        raise NotImplementedError
        new_axes = list(self._obj_with_exclusions.axes)
        new_axes[self.axis] = self.grouper.result_index

        result = Panel(output, index=self.grouper.result_index,
                       columns=output_keys)

        if self.axis > 0:
            result = result.swapaxes(0, self.axis)

        return result


class NDArrayGroupBy(GroupBy):
    pass


#----------------------------------------------------------------------
# Grouping generator for BlockManager

def generate_groups(data, group_index, ngroups, axis=0, factory=lambda x: x):
    """
    Parameters
    ----------
    data : BlockManager

    Returns
    -------
    generator
    """
    group_index = com._ensure_int64(group_index)

    indexer = lib.groupsort_indexer(group_index, ngroups)[0]
    group_index = com.ndtake(group_index, indexer)

    if isinstance(data, BlockManager):
        # this is sort of wasteful but...
        sorted_axis = data.axes[axis].take(indexer)
        sorted_data = data.reindex_axis(sorted_axis, axis=axis)
    if isinstance(data, Series):
        sorted_axis = data.index.take(indexer)
        sorted_data = data.reindex(sorted_axis)
    elif isinstance(data, DataFrame):
        sorted_data = data.take(indexer, axis=axis)

    if isinstance(sorted_data, DataFrame):
        def _get_slice(slob):
            if axis == 0:
                return sorted_data[slob]
            else:
                return sorted_data.ix[:, slob]
    elif isinstance(sorted_data, BlockManager):
        def _get_slice(slob):
            return factory(sorted_data.get_slice(slob, axis=axis))
    elif isinstance(sorted_data, Series):
        def _get_slice(slob):
            return sorted_data._get_values(slob)
    else:  # pragma: no cover
        def _get_slice(slob):
            return sorted_data[slob]

    starts, ends = lib.generate_slices(group_index, ngroups)

    for i, (start, end) in enumerate(zip(starts, ends)):
        # Since I'm now compressing the group ids, it's now not "possible" to
        # produce empty slices because such groups would not be observed in the
        # data
        assert(start < end)
        yield i, _get_slice(slice(start, end))

def get_group_index(label_list, shape):
    """
    For the particular label_list, gets the offsets into the hypothetical list
    representing the totally ordered cartesian product of all possible label
    combinations.
    """
    if len(label_list) == 1:
        return label_list[0]

    n = len(label_list[0])
    group_index = np.zeros(n, dtype=np.int64)
    mask = np.zeros(n, dtype=bool)
    for i in xrange(len(shape)):
        stride = np.prod([x for x in shape[i+1:]], dtype=np.int64)
        group_index += com._ensure_int64(label_list[i]) * stride
        mask |= label_list[i] < 0

    np.putmask(group_index, mask, -1)
    return group_index

_INT64_MAX = np.iinfo(np.int64).max
def _int64_overflow_possible(shape):
    the_prod = 1L
    for x in shape:
        the_prod *= long(x)

    return the_prod >= _INT64_MAX

def decons_group_index(comp_labels, shape):
    # reconstruct labels
    label_list = []
    factor = 1
    y = 0
    x = comp_labels
    for i in reversed(xrange(len(shape))):
        labels = (x - y) % (factor * shape[i]) // factor
        np.putmask(labels, comp_labels < 0, -1)
        label_list.append(labels)
        y = labels * factor
        factor *= shape[i]
    return label_list[::-1]


def _indexer_from_factorized(labels, shape, compress=True):
    if _int64_overflow_possible(shape):
        indexer = np.lexsort(np.array(labels[::-1]))
        return indexer

    group_index = get_group_index(labels, shape)

    if compress:
        comp_ids, obs_ids = _compress_group_index(group_index)
        max_group = len(obs_ids)
    else:
        comp_ids = group_index
        max_group = np.prod(shape)

    indexer, _ = lib.groupsort_indexer(comp_ids.astype(np.int64), max_group)

    return indexer


def _lexsort_indexer(keys):
    labels = []
    shape = []
    for key in keys:
        rizer = lib.Factorizer(len(key))

        if not key.dtype == np.object_:
            key = key.astype('O')

        ids, _ = rizer.factorize(key, sort=True)
        labels.append(ids)
        shape.append(len(rizer.uniques))
    return _indexer_from_factorized(labels, shape)

class _KeyMapper(object):
    """
    Ease my suffering. Map compressed group id -> key tuple
    """
    def __init__(self, comp_ids, ngroups, labels, levels):
        self.levels = levels
        self.labels = labels
        self.comp_ids = comp_ids.astype(np.int64)

        self.k = len(labels)
        self.tables = [lib.Int64HashTable(ngroups) for _ in range(self.k)]

        self._populate_tables()

    def _populate_tables(self):
        for labs, table in zip(self.labels, self.tables):
            table.map(self.comp_ids, labs.astype(np.int64))

    def get_key(self, comp_id):
        return tuple(level[table.get_item(comp_id)]
                     for table, level in zip(self.tables, self.levels))


def _get_indices_dict(label_list, keys):
    shape = [len(x) for x in keys]
    group_index = get_group_index(label_list, shape)

    sorter, _ = lib.groupsort_indexer(com._ensure_int64(group_index),
                                      np.prod(shape))

    sorter_int = com._ensure_platform_int(sorter)

    sorted_labels = [lab.take(sorter_int) for lab in label_list]
    group_index = group_index.take(sorter_int)

    return lib.indices_fast(sorter, group_index, keys, sorted_labels)

#----------------------------------------------------------------------
# sorting levels...cleverly?

def _compress_group_index(group_index, sort=True):
    """
    Group_index is offsets into cartesian product of all possible labels. This
    space can be huge, so this function compresses it, by computing offsets
    (comp_ids) into the list of unique labels (obs_group_ids).
    """

    uniques = []
    table = lib.Int64HashTable(len(group_index))

    group_index = com._ensure_int64(group_index)

    # note, group labels come out ascending (ie, 1,2,3 etc)
    comp_ids = table.get_labels_groupby(group_index, uniques)

    # these are the unique ones we observed, in the order we observed them
    obs_group_ids = np.array(uniques, dtype=np.int64)

    if sort and len(obs_group_ids) > 0:
        obs_group_ids, comp_ids = _reorder_by_uniques(obs_group_ids, comp_ids)

    return comp_ids, obs_group_ids

def _reorder_by_uniques(uniques, labels):
    # sorter is index where elements ought to go
    sorter = uniques.argsort()

    # reverse_indexer is where elements came from
    reverse_indexer = np.empty(len(sorter), dtype=np.int64)
    reverse_indexer.put(sorter, np.arange(len(sorter)))

    mask = labels < 0

    # move labels to right locations (ie, unsort ascending labels)
    labels = com.ndtake(reverse_indexer, labels)
    np.putmask(labels, mask, -1)

    # sort observed ids
    uniques = com.ndtake(uniques, sorter)

    return uniques, labels

import __builtin__

_func_table = {
    __builtin__.sum: np.sum
}

_cython_table = {
    __builtin__.sum: 'sum',
    np.sum: 'sum',
    np.mean: 'mean',
    np.prod: 'prod',
    np.std: 'std',
    np.var: 'var'
}

def _intercept_function(func):
    return _func_table.get(func, func)

def _intercept_cython(func):
    return _cython_table.get(func)

def _groupby_indices(values):
    return lib.groupby_indices(com._ensure_object(values))

def numpy_groupby(data, labels, axis=0):
    s = np.argsort(labels)
    keys, inv = np.unique(labels, return_inverse=True)
    i = inv.take(s)
    groups_at = np.where(i != np.concatenate(([-1], i[:-1])))[0]
    ordered_data = data.take(s, axis=axis)
    group_sums = np.add.reduceat(ordered_data, groups_at, axis=axis)

    return group_sums

#-----------------------------------------------------------------------
# Helper functions


from pandas.util import py3compat
import sys

def install_ipython_completers():  # pragma: no cover
    """Register the DataFrame type with IPython's tab completion machinery, so
    that it knows about accessing column names as attributes."""
    from IPython.utils.generics import complete_object

    @complete_object.when_type(DataFrameGroupBy)
    def complete_dataframe(obj, prev_completions):
        return prev_completions + [c for c in obj.obj.columns \
                    if isinstance(c, basestring) and py3compat.isidentifier(c)]


# Importing IPython brings in about 200 modules, so we want to avoid it unless
# we're in IPython (when those modules are loaded anyway).
if "IPython" in sys.modules:  # pragma: no cover
    try:
        install_ipython_completers()
    except Exception:
        pass


