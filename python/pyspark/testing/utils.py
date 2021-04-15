#
# Licensed to the Apache Software Foundation (ASF) under one or more
# contributor license agreements.  See the NOTICE file distributed with
# this work for additional information regarding copyright ownership.
# The ASF licenses this file to You under the Apache License, Version 2.0
# (the "License"); you may not use this file except in compliance with
# the License.  You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
import functools
import glob
import os
import shutil
import struct
import sys
import unittest
import tempfile
import warnings
from contextlib import contextmanager
from distutils.version import LooseVersion
from time import time, sleep

import pandas as pd
from pandas.api.types import is_list_like
from pandas.testing import assert_frame_equal, assert_index_equal, assert_series_equal

from pyspark import SparkContext, SparkConf
from pyspark import pandas as ps
from pyspark.testing.sqlutils import SQLTestUtils
from pyspark.pandas.frame import DataFrame
from pyspark.pandas.indexes import Index
from pyspark.pandas.series import Series
from pyspark.pandas.utils import default_session, SPARK_CONF_ARROW_ENABLED


have_scipy = False
have_numpy = False
try:
    import scipy.sparse  # noqa: F401
    have_scipy = True
except:
    # No SciPy, but that's okay, we'll skip those tests
    pass
try:
    import numpy as np  # noqa: F401
    have_numpy = True
except:
    # No NumPy, but that's okay, we'll skip those tests
    pass

tabulate_requirement_message = None
try:
    from tabulate import tabulate  # noqa: F401
except ImportError as e:
    # If tabulate requirement is not satisfied, skip related tests.
    tabulate_requirement_message = str(e)
have_tabulate = tabulate_requirement_message is None

matplotlib_requirement_message = None
try:
    import matplotlib  # type: ignore # noqa: F401
except ImportError as e:
    # If matplotlib requirement is not satisfied, skip related tests.
    matplotlib_requirement_message = str(e)
have_matplotlib = matplotlib_requirement_message is None

plotly_requirement_message = None
try:
    import plotly  # type: ignore # noqa: F401
except ImportError as e:
    # If plotly requirement is not satisfied, skip related tests.
    plotly_requirement_message = str(e)
have_plotly = plotly_requirement_message is None

SPARK_HOME = os.environ["SPARK_HOME"]


def read_int(b):
    return struct.unpack("!i", b)[0]


def write_int(i):
    return struct.pack("!i", i)


def eventually(condition, timeout=30.0, catch_assertions=False):
    """
    Wait a given amount of time for a condition to pass, else fail with an error.
    This is a helper utility for PySpark tests.

    Parameters
    ----------
    condition : function
        Function that checks for termination conditions. condition() can return:
            - True: Conditions met. Return without error.
            - other value: Conditions not met yet. Continue. Upon timeout,
              include last such value in error message.
              Note that this method may be called at any time during
              streaming execution (e.g., even before any results
              have been created).
    timeout : int
        Number of seconds to wait.  Default 30 seconds.
    catch_assertions : bool
        If False (default), do not catch AssertionErrors.
        If True, catch AssertionErrors; continue, but save
        error to throw upon timeout.
    """
    start_time = time()
    lastValue = None
    while time() - start_time < timeout:
        if catch_assertions:
            try:
                lastValue = condition()
            except AssertionError as e:
                lastValue = e
        else:
            lastValue = condition()
        if lastValue is True:
            return
        sleep(0.01)
    if isinstance(lastValue, AssertionError):
        raise lastValue
    else:
        raise AssertionError(
            "Test failed due to timeout after %g sec, with last condition returning: %s"
            % (timeout, lastValue))


class QuietTest(object):
    def __init__(self, sc):
        self.log4j = sc._jvm.org.apache.log4j

    def __enter__(self):
        self.old_level = self.log4j.LogManager.getRootLogger().getLevel()
        self.log4j.LogManager.getRootLogger().setLevel(self.log4j.Level.FATAL)

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.log4j.LogManager.getRootLogger().setLevel(self.old_level)


class PySparkTestCase(unittest.TestCase):

    def setUp(self):
        self._old_sys_path = list(sys.path)
        class_name = self.__class__.__name__
        self.sc = SparkContext('local[4]', class_name)

    def tearDown(self):
        self.sc.stop()
        sys.path = self._old_sys_path


class ReusedPySparkTestCase(unittest.TestCase):

    @classmethod
    def conf(cls):
        """
        Override this in subclasses to supply a more specific conf
        """
        return SparkConf()

    @classmethod
    def setUpClass(cls):
        cls.sc = SparkContext('local[4]', cls.__name__, conf=cls.conf())

    @classmethod
    def tearDownClass(cls):
        cls.sc.stop()


class ByteArrayOutput(object):
    def __init__(self):
        self.buffer = bytearray()

    def write(self, b):
        self.buffer += b

    def close(self):
        pass


def search_jar(project_relative_path, sbt_jar_name_prefix, mvn_jar_name_prefix):
    # Note that 'sbt_jar_name_prefix' and 'mvn_jar_name_prefix' are used since the prefix can
    # vary for SBT or Maven specifically. See also SPARK-26856
    project_full_path = os.path.join(
        os.environ["SPARK_HOME"], project_relative_path)

    # We should ignore the following jars
    ignored_jar_suffixes = ("javadoc.jar", "sources.jar", "test-sources.jar", "tests.jar")

    # Search jar in the project dir using the jar name_prefix for both sbt build and maven
    # build because the artifact jars are in different directories.
    sbt_build = glob.glob(os.path.join(
        project_full_path, "target/scala-*/%s*.jar" % sbt_jar_name_prefix))
    maven_build = glob.glob(os.path.join(
        project_full_path, "target/%s*.jar" % mvn_jar_name_prefix))
    jar_paths = sbt_build + maven_build
    jars = [jar for jar in jar_paths if not jar.endswith(ignored_jar_suffixes)]

    if not jars:
        return None
    elif len(jars) > 1:
        raise Exception("Found multiple JARs: %s; please remove all but one" % (", ".join(jars)))
    else:
        return jars[0]


# Utilities below are used mainly in pyspark/pandas
class ReusedSQLTestCase(unittest.TestCase, SQLTestUtils):
    @classmethod
    def setUpClass(cls):
        cls.spark = default_session()
        cls.spark.conf.set(SPARK_CONF_ARROW_ENABLED, True)

    @classmethod
    def tearDownClass(cls):
        # We don't stop Spark session to reuse across all tests.
        # The Spark session will be started and stopped at PyTest session level.
        # Please see databricks/koalas/conftest.py.
        pass

    def assertPandasEqual(self, left, right, check_exact=True):
        if isinstance(left, pd.DataFrame) and isinstance(right, pd.DataFrame):
            try:
                if LooseVersion(pd.__version__) >= LooseVersion("1.1"):
                    kwargs = dict(check_freq=False)
                else:
                    kwargs = dict()

                assert_frame_equal(
                    left,
                    right,
                    check_index_type=("equiv" if len(left.index) > 0 else False),
                    check_column_type=("equiv" if len(left.columns) > 0 else False),
                    check_exact=check_exact,
                    **kwargs
                )
            except AssertionError as e:
                msg = (
                    str(e)
                    + "\n\nLeft:\n%s\n%s" % (left, left.dtypes)
                    + "\n\nRight:\n%s\n%s" % (right, right.dtypes)
                )
                raise AssertionError(msg) from e
        elif isinstance(left, pd.Series) and isinstance(right, pd.Series):
            try:
                if LooseVersion(pd.__version__) >= LooseVersion("1.1"):
                    kwargs = dict(check_freq=False)
                else:
                    kwargs = dict()

                assert_series_equal(
                    left,
                    right,
                    check_index_type=("equiv" if len(left.index) > 0 else False),
                    check_exact=check_exact,
                    **kwargs
                )
            except AssertionError as e:
                msg = (
                    str(e)
                    + "\n\nLeft:\n%s\n%s" % (left, left.dtype)
                    + "\n\nRight:\n%s\n%s" % (right, right.dtype)
                )
                raise AssertionError(msg) from e
        elif isinstance(left, pd.Index) and isinstance(right, pd.Index):
            try:
                assert_index_equal(left, right, check_exact=check_exact)
            except AssertionError as e:
                msg = (
                    str(e)
                    + "\n\nLeft:\n%s\n%s" % (left, left.dtype)
                    + "\n\nRight:\n%s\n%s" % (right, right.dtype)
                )
                raise AssertionError(msg) from e
        else:
            raise ValueError("Unexpected values: (%s, %s)" % (left, right))

    def assertPandasAlmostEqual(self, left, right):
        """
        This function checks if given pandas objects approximately same,
        which means the conditions below:
          - Both objects are nullable
          - Compare floats rounding to the number of decimal places, 7 after
            dropping missing values (NaN, NaT, None)
        """
        if isinstance(left, pd.DataFrame) and isinstance(right, pd.DataFrame):
            msg = (
                "DataFrames are not almost equal: "
                + "\n\nLeft:\n%s\n%s" % (left, left.dtypes)
                + "\n\nRight:\n%s\n%s" % (right, right.dtypes)
            )
            self.assertEqual(left.shape, right.shape, msg=msg)
            for lcol, rcol in zip(left.columns, right.columns):
                self.assertEqual(lcol, rcol, msg=msg)
                for lnull, rnull in zip(left[lcol].isnull(), right[rcol].isnull()):
                    self.assertEqual(lnull, rnull, msg=msg)
                for lval, rval in zip(left[lcol].dropna(), right[rcol].dropna()):
                    self.assertAlmostEqual(lval, rval, msg=msg)
            self.assertEqual(left.columns.names, right.columns.names, msg=msg)
        elif isinstance(left, pd.Series) and isinstance(right, pd.Series):
            msg = (
                "Series are not almost equal: "
                + "\n\nLeft:\n%s\n%s" % (left, left.dtype)
                + "\n\nRight:\n%s\n%s" % (right, right.dtype)
            )
            self.assertEqual(left.name, right.name, msg=msg)
            self.assertEqual(len(left), len(right), msg=msg)
            for lnull, rnull in zip(left.isnull(), right.isnull()):
                self.assertEqual(lnull, rnull, msg=msg)
            for lval, rval in zip(left.dropna(), right.dropna()):
                self.assertAlmostEqual(lval, rval, msg=msg)
        elif isinstance(left, pd.MultiIndex) and isinstance(right, pd.MultiIndex):
            msg = (
                "MultiIndices are not almost equal: "
                + "\n\nLeft:\n%s\n%s" % (left, left.dtype)
                + "\n\nRight:\n%s\n%s" % (right, right.dtype)
            )
            self.assertEqual(len(left), len(right), msg=msg)
            for lval, rval in zip(left, right):
                self.assertAlmostEqual(lval, rval, msg=msg)
        elif isinstance(left, pd.Index) and isinstance(right, pd.Index):
            msg = (
                "Indices are not almost equal: "
                + "\n\nLeft:\n%s\n%s" % (left, left.dtype)
                + "\n\nRight:\n%s\n%s" % (right, right.dtype)
            )
            self.assertEqual(len(left), len(right), msg=msg)
            for lnull, rnull in zip(left.isnull(), right.isnull()):
                self.assertEqual(lnull, rnull, msg=msg)
            for lval, rval in zip(left.dropna(), right.dropna()):
                self.assertAlmostEqual(lval, rval, msg=msg)
        else:
            raise ValueError("Unexpected values: (%s, %s)" % (left, right))

    def assert_eq(self, left, right, check_exact=True, almost=False):
        """
        Asserts if two arbitrary objects are equal or not. If given objects are Koalas DataFrame
        or Series, they are converted into pandas' and compared.

        :param left: object to compare
        :param right: object to compare
        :param check_exact: if this is False, the comparison is done less precisely.
        :param almost: if this is enabled, the comparison is delegated to `unittest`'s
                       `assertAlmostEqual`. See its documentation for more details.
        """
        lobj = self._to_pandas(left)
        robj = self._to_pandas(right)
        if isinstance(lobj, (pd.DataFrame, pd.Series, pd.Index)):
            if almost:
                self.assertPandasAlmostEqual(lobj, robj)
            else:
                self.assertPandasEqual(lobj, robj, check_exact=check_exact)
        elif is_list_like(lobj) and is_list_like(robj):
            self.assertTrue(len(left) == len(right))
            for litem, ritem in zip(left, right):
                self.assert_eq(litem, ritem, check_exact=check_exact, almost=almost)
        elif (lobj is not None and pd.isna(lobj)) and (robj is not None and pd.isna(robj)):
            pass
        else:
            if almost:
                self.assertAlmostEqual(lobj, robj)
            else:
                self.assertEqual(lobj, robj)

    @staticmethod
    def _to_pandas(obj):
        if isinstance(obj, (DataFrame, Series, Index)):
            return obj.to_pandas()
        else:
            return obj


class TestUtils(object):
    @contextmanager
    def temp_dir(self):
        tmp = tempfile.mkdtemp()
        try:
            yield tmp
        finally:
            shutil.rmtree(tmp)

    @contextmanager
    def temp_file(self):
        with self.temp_dir() as tmp:
            yield tempfile.mktemp(dir=tmp)


class ComparisonTestBase(ReusedSQLTestCase):
    @property
    def kdf(self):
        return ps.from_pandas(self.pdf)

    @property
    def pdf(self):
        return self.kdf.to_pandas()


def compare_both(f=None, almost=True):

    if f is None:
        return functools.partial(compare_both, almost=almost)
    elif isinstance(f, bool):
        return functools.partial(compare_both, almost=f)

    @functools.wraps(f)
    def wrapped(self):
        if almost:
            compare = self.assertPandasAlmostEqual
        else:
            compare = self.assertPandasEqual

        for result_pandas, result_spark in zip(f(self, self.pdf), f(self, self.kdf)):
            compare(result_pandas, result_spark.to_pandas())

    return wrapped


@contextmanager
def assert_produces_warning(
    expected_warning=Warning,
    filter_level="always",
    check_stacklevel=True,
    raise_on_extra_warnings=True,
):
    """
    Context manager for running code expected to either raise a specific
    warning, or not raise any warnings. Verifies that the code raises the
    expected warning, and that it does not raise any other unexpected
    warnings. It is basically a wrapper around ``warnings.catch_warnings``.

    Notes
    -----
    Replicated from pandas._testing.

    Parameters
    ----------
    expected_warning : {Warning, False, None}, default Warning
        The type of Exception raised. ``exception.Warning`` is the base
        class for all warnings. To check that no warning is returned,
        specify ``False`` or ``None``.
    filter_level : str or None, default "always"
        Specifies whether warnings are ignored, displayed, or turned
        into errors.
        Valid values are:
        * "error" - turns matching warnings into exceptions
        * "ignore" - discard the warning
        * "always" - always emit a warning
        * "default" - print the warning the first time it is generated
          from each location
        * "module" - print the warning the first time it is generated
          from each module
        * "once" - print the warning the first time it is generated
    check_stacklevel : bool, default True
        If True, displays the line that called the function containing
        the warning to show were the function is called. Otherwise, the
        line that implements the function is displayed.
    raise_on_extra_warnings : bool, default True
        Whether extra warnings not of the type `expected_warning` should
        cause the test to fail.

    Examples
    --------
    >>> import warnings
    >>> with assert_produces_warning():
    ...     warnings.warn(UserWarning())
    ...
    >>> with assert_produces_warning(False): # doctest: +SKIP
    ...     warnings.warn(RuntimeWarning())
    ...
    Traceback (most recent call last):
        ...
    AssertionError: Caused unexpected warning(s): ['RuntimeWarning'].
    >>> with assert_produces_warning(UserWarning): # doctest: +SKIP
    ...     warnings.warn(RuntimeWarning())
    Traceback (most recent call last):
        ...
    AssertionError: Did not see expected warning of class 'UserWarning'
    ..warn:: This is *not* thread-safe.
    """
    __tracebackhide__ = True

    with warnings.catch_warnings(record=True) as w:

        saw_warning = False
        warnings.simplefilter(filter_level)
        yield w
        extra_warnings = []

        for actual_warning in w:
            if expected_warning and issubclass(actual_warning.category, expected_warning):
                saw_warning = True

                if check_stacklevel and issubclass(
                        actual_warning.category, (FutureWarning, DeprecationWarning)
                ):
                    from inspect import getframeinfo, stack

                    caller = getframeinfo(stack()[2][0])
                    msg = (
                        "Warning not set with correct stacklevel. ",
                        "File where warning is raised: {} != ".format(actual_warning.filename),
                        "{}. Warning message: {}".format(caller.filename, actual_warning.message),
                    )
                    assert actual_warning.filename == caller.filename, msg
            else:
                extra_warnings.append(
                    (
                        actual_warning.category.__name__,
                        actual_warning.message,
                        actual_warning.filename,
                        actual_warning.lineno,
                    )
                )
        if expected_warning:
            msg = "Did not see expected warning of class {}".format(repr(expected_warning.__name__))
            assert saw_warning, msg
        if raise_on_extra_warnings and extra_warnings:
            raise AssertionError("Caused unexpected warning(s): {}".format(repr(extra_warnings)))
