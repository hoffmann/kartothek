import os
from datetime import date, datetime
from distutils.version import LooseVersion

import numpy as np
import pandas as pd
import pandas.testing as pdt
import pandas.util.testing as pdtest
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import six
import storefact
from pyarrow.parquet import ParquetFile

from kartothek.serialization import DataFrameSerializer, ParquetSerializer
from kartothek.serialization._util import _check_contains_null

ARROW_LARGER_EQ_0130 = LooseVersion(pa.__version__) >= "0.13.0"


@pytest.fixture
def reference_store():
    path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.realpath(__file__))),
        "..",
        "reference-data",
        "pyarrow-bugs",
    )
    return storefact.get_store_from_url("hfs://{}".format(path))


def test_timestamp_us(store):
    # test that a df with us precision round-trips using parquet
    ts = datetime(2000, 1, 1, 15, 23, 24, 123456)
    df = pd.DataFrame({"ts": [ts]})
    serialiser = ParquetSerializer()
    key = serialiser.store(store, "prefix", df)
    pdtest.assert_frame_equal(DataFrameSerializer.restore_dataframe(store, key), df)


def test_pyarrow_07992(store):
    key = "test.parquet"
    df = pd.DataFrame({"a": [1]})
    table = pa.Table.from_pandas(df)
    meta = b"""{
        "pandas_version": "0.20.3",
        "index_columns": ["__index_level_0__"],
        "columns": [
            {"metadata": null, "name": "a", "numpy_type": "int64", "pandas_type": "int64"},
            {"metadata": null, "name": null, "numpy_type": "int64", "pandas_type": "int64"}
        ],
        "column_indexes": [
            {"metadata": null, "name": null, "numpy_type": "object", "pandas_type": "string"}
        ]
    }"""
    table = table.replace_schema_metadata({b"pandas": meta})
    buf = pa.BufferOutputStream()
    pq.write_table(table, buf)
    store.put(key, buf.getvalue().to_pybytes())
    pdtest.assert_frame_equal(DataFrameSerializer.restore_dataframe(store, key), df)


def test_scd_4440(store):
    key = "test.parquet"
    df = pd.DataFrame({"a": [1]})
    table = pa.Table.from_pandas(df)
    meta = b"""{
        "pandas_version": "0.20.3",
        "index_columns": ["__index_level_0__"],
        "columns": [
            {"metadata": null, "name": "a", "numpy_type": "int64", "pandas_type": "int64"}
        ]
    }"""
    table = table.replace_schema_metadata({b"pandas": meta})
    buf = pa.BufferOutputStream()
    pq.write_table(table, buf)
    store.put(key, buf.getvalue().to_pybytes())
    pdtest.assert_frame_equal(DataFrameSerializer.restore_dataframe(store, key), df)


@pytest.fixture(params=[1, 5, None])
def chunk_size(request, mocker):
    chunk_size = request.param
    if chunk_size == 1:
        # Test for a chunk size of one and mock the filter_df call. This way we can ensure that
        # the predicate for IO is properly evaluated and pushed down
        mocker.patch(
            "kartothek.serialization._parquet.filter_df",
            new=lambda x, *args, **kwargs: x,
        )
    return chunk_size


@pytest.mark.parametrize("use_categorical", [True, False])
def test_rowgroup_writing(store, use_categorical, chunk_size):
    df = pd.DataFrame({"string": ["abc", "affe", "banane", "buchstabe"]})
    serialiser = ParquetSerializer(chunk_size=2)
    # Arrow 0.9.0 has a bug in writing categorical columns to more than a single
    # RowGroup: "ArrowIOError: Column 2 had 2 while previous column had 4".
    # We have special handling for that in pandas-serialiser that should be
    # removed once we switch to 0.10.0
    if use_categorical:
        df_write = df.astype({"string": "category"})
    else:
        df_write = df
    key = serialiser.store(store, "prefix", df_write)

    parquet_file = ParquetFile(store.open(key))
    assert parquet_file.num_row_groups == 2


_INT_TYPES = ["int8", "int16", "int32", "int64"]

# uint64 will fail since numexpr cannot safe case from uint64 to int64
_UINT_TYPES = ["uint8", "uint16", "uint32"]

_FLOAT_TYPES = ["float32", "float64"]

_STR_TYPES = ["unicode", "bytes"]

_DATE_TYPES = ["date"]
_DATETIME_TYPES = ["datetime64"]


def _validate_predicate_pushdown(df, column, value, store, chunk_size):

    serialiser = ParquetSerializer(chunk_size=chunk_size)
    key = serialiser.store(store, "prefix", df)

    predicates = [[(column, "==", value)]]

    df_restored = serialiser.restore_dataframe(store, key, predicates=predicates)
    # date objects are converted to datetime in pyarrow
    df_restored["date"] = df_restored["date"].dt.date

    expected = df.iloc[[3]]
    # ARROW-5138 index isn't preserved when doing predicate pushdown
    if ARROW_LARGER_EQ_0130:
        pdt.assert_frame_equal(
            df_restored.reset_index(drop=True), expected.reset_index(drop=True)
        )
    else:
        pdt.assert_frame_equal(df_restored, expected)


@pytest.mark.parametrize("column", _INT_TYPES)
@pytest.mark.parametrize(
    "input_values",
    [
        (3, None),
        (3.0, TypeError),
        (u"3", TypeError),
        (u"3.0", TypeError),
        (b"3", TypeError),
        (b"3.0", TypeError),
    ],
)
def test_predicate_evaluation_integer(
    store, dataframe_not_nested, column, input_values, chunk_size
):
    value, exception = input_values
    if exception:
        with pytest.raises(exception):
            _validate_predicate_pushdown(
                dataframe_not_nested, column, value, store, chunk_size
            )
    else:
        _validate_predicate_pushdown(
            dataframe_not_nested, column, value, store, chunk_size
        )


@pytest.mark.parametrize("column", _UINT_TYPES)
@pytest.mark.parametrize(
    "input_values",
    [
        (3, None),
        (3.0, TypeError),
        (u"3", TypeError),
        (u"3.0", TypeError),
        (b"3", TypeError),
        (b"3.0", TypeError),
    ],
)
def test_predicate_evaluation_unsigned_integer(
    store, dataframe_not_nested, column, input_values, chunk_size
):
    value, exception = input_values
    if exception:
        with pytest.raises(exception):
            _validate_predicate_pushdown(
                dataframe_not_nested, column, value, store, chunk_size
            )
    else:
        _validate_predicate_pushdown(
            dataframe_not_nested, column, value, store, chunk_size
        )


@pytest.mark.parametrize("column", _FLOAT_TYPES)
@pytest.mark.parametrize(
    "input_values",
    [
        (3, TypeError),
        (3.0, None),
        (u"3", TypeError),
        (u"3.0", TypeError),
        (b"3", TypeError),
        (b"3.0", TypeError),
    ],
)
def test_predicate_evaluation_float(
    store, dataframe_not_nested, column, input_values, chunk_size
):
    value, exception = input_values
    if exception:
        with pytest.raises(exception):
            _validate_predicate_pushdown(
                dataframe_not_nested, column, value, store, chunk_size
            )
    else:
        _validate_predicate_pushdown(
            dataframe_not_nested, column, value, store, chunk_size
        )


@pytest.mark.parametrize("column", _STR_TYPES)
@pytest.mark.parametrize(
    "input_values", [(3, TypeError), (3.0, TypeError), (u"3", None), (b"3", None)]
)
def test_predicate_evaluation_string(
    store, dataframe_not_nested, column, input_values, chunk_size
):
    value, exception = input_values
    if exception:
        with pytest.raises(exception):
            _validate_predicate_pushdown(
                dataframe_not_nested, column, value, store, chunk_size
            )
    else:
        _validate_predicate_pushdown(
            dataframe_not_nested, column, value, store, chunk_size
        )


@pytest.mark.parametrize("column", _DATE_TYPES)
@pytest.mark.parametrize(
    "input_values",
    [
        # it's the fifth due to the day % 31 in the testdata
        (date(2018, 1, 5), None),
        (u"2018-01-05", None),
        (b"2018-01-05", None),
        (datetime(2018, 1, 1, 1, 1), TypeError),
        (3, TypeError),
        (3.0, TypeError),
        (u"3", ValueError),
        (u"3.0", ValueError),
        (b"3", ValueError),
        (b"3.0", ValueError),
    ],
)
def test_predicate_evaluation_date(
    store, dataframe_not_nested, column, input_values, chunk_size
):
    value, exception = input_values
    if exception:
        with pytest.raises(exception):
            _validate_predicate_pushdown(
                dataframe_not_nested, column, value, store, chunk_size
            )
    else:
        _validate_predicate_pushdown(
            dataframe_not_nested, column, value, store, chunk_size
        )


@pytest.mark.parametrize("column", _DATETIME_TYPES)
@pytest.mark.parametrize(
    "input_values",
    [
        (datetime(2018, 1, 5), None),
        (np.datetime64(datetime(2018, 1, 5)), None),
        (pd.Timestamp(datetime(2018, 1, 5)), None),
        (np.datetime64(datetime(2018, 1, 5), "s"), None),
        (np.datetime64(datetime(2018, 1, 5), "ms"), None),
        (np.datetime64(datetime(2018, 1, 5), "us"), None),
        (np.datetime64(datetime(2018, 1, 5), "ns"), None),
        (date(2018, 1, 4), TypeError),
        (u"2018-01-04", TypeError),
        (b"2018-01-04", TypeError),
        (1, TypeError),
        (1.0, TypeError),
    ],
)
def test_predicate_evaluation_datetime(
    store, dataframe_not_nested, column, input_values, chunk_size
):
    value, exception = input_values
    if exception:
        with pytest.raises(exception):
            _validate_predicate_pushdown(
                dataframe_not_nested, column, value, store, chunk_size
            )
    else:
        _validate_predicate_pushdown(
            dataframe_not_nested, column, value, store, chunk_size
        )


def test_ensure_binaries(binary_value):
    assert isinstance(binary_value, six.binary_type)


def test_pushdown_binaries(store, dataframe_not_nested, binary_value, chunk_size):
    if _check_contains_null(binary_value):
        pytest.xfail("Null-terminated binary strings are not supported")
    serialiser = ParquetSerializer(chunk_size=chunk_size)
    key = serialiser.store(store, "prefix", dataframe_not_nested)

    predicates = [[("bytes", "==", binary_value)]]

    df_restored = serialiser.restore_dataframe(store, key, predicates=predicates)
    assert len(df_restored) == 1
    assert df_restored.iloc[0].bytes == binary_value


@pytest.mark.xfail(
    reason="Requires parquet-cpp 1.5.0."
)
def test_pushdown_null_itermediate(store):
    binary = b"\x8f\xb6\xe5@\x90\xdc\x11\xe8\x00\xae\x02B\xac\x12\x01\x06"
    df = pd.DataFrame({"byte_with_null": [binary]})
    serialiser = ParquetSerializer(chunk_size=1)
    key = serialiser.store(store, "key", df)
    predicate = [[("byte_with_null", "==", binary)]]
    restored = serialiser.restore_dataframe(store, key, predicates=predicate)
    pdt.assert_frame_equal(restored, df)


@pytest.mark.parametrize("chunk_size", [None, 1])
def test_date_as_object(store, chunk_size):
    ser = ParquetSerializer(chunk_size=chunk_size)
    df = pd.DataFrame({"date": [date(2000, 1, 1), date(2000, 1, 2)]})
    key = ser.store(store, "key", df)
    restored_df = ser.restore_dataframe(
        store, key, categories=["date"], date_as_object=True
    )
    categories = pd.Series([date(2000, 1, 1), date(2000, 1, 2)])
    expected_df = pd.DataFrame({"date": pd.Categorical(categories)})
    # expected_df.date = expected_df.date.cat.rename_categories([date(2000, 1, 1)])
    pdt.assert_frame_equal(restored_df, expected_df)

    restored_df = ser.restore_dataframe(
        store, key, date_as_object=True, predicates=[[("date", "==", "2000-01-01")]]
    )
    expected_df = pd.DataFrame({"date": [date(2000, 1, 1)]})
    pdt.assert_frame_equal(restored_df, expected_df)


@pytest.mark.parametrize("chunk_size", [None, 1])
def test_predicate_not_in_columns(store, chunk_size):
    ser = ParquetSerializer(chunk_size=chunk_size)
    df = pd.DataFrame(
        {
            "date": [date(2000, 1, 1), date(2000, 1, 2), date(2000, 1, 2)],
            "col": [1, 2, 1],
        }
    )
    key = ser.store(store, "key", df)
    restored_df = ser.restore_dataframe(
        store, key, columns=[], predicates=[[("col", "==", 1)]]
    )
    if chunk_size:
        expected_df = pd.DataFrame(index=[0, 1])
    else:
        expected_df = pd.DataFrame(index=[0, 2])

    pdt.assert_frame_equal(restored_df, expected_df)


def test_read_empty_file_with_predicates(store):
    ser = ParquetSerializer()
    df = pd.DataFrame(dict(col=pd.Series([], dtype=str)))
    key = ser.store(store, "key", df)
    restored_df = ser.restore_dataframe(
        store, key, columns=["col"], predicates=[[("col", "==", "1")]]
    )
    pdt.assert_frame_equal(restored_df, df)


@pytest.mark.parametrize("predicate_pushdown_to_io", [True, False])
def test_int64_statistics_overflow(reference_store, predicate_pushdown_to_io):
    # Test case for ARROW-5166
    ser = ParquetSerializer()

    v = 705449463447499237
    predicates = [[("x", "==", v)]]
    result = ser.restore_dataframe(
        reference_store,
        "int64_statistics_overflow.parquet",
        predicate_pushdown_to_io=predicate_pushdown_to_io,
        predicates=predicates,
    )
    assert not result.empty
    assert (result["x"] == v).all()
