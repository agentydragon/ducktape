"""Tests for Python AST analyzer."""

from pathlib import Path

from ducktape_llm_common.claude_linter_v2.linters.python_ast import PythonASTAnalyzer

TEST_FILE = Path("/tmp/test.py")


class TestBareExcept:
    """Test bare except detection."""

    def test_detects_bare_except(self):
        """Test that bare except is detected."""
        code = """
try:
    x = 1/0
except:
    pass
"""
        analyzer = PythonASTAnalyzer(bare_except=True, getattr_setattr=False, barrel_init=False)
        violations = analyzer.analyze_code(code, TEST_FILE)

        assert len(violations) == 1
        assert violations[0].line == 4
        assert "bare except" in violations[0].message.lower()
        assert violations[0].rule == "bare_except"

    def test_allows_specific_except(self):
        """Test that specific exceptions are allowed."""
        code = """
try:
    x = 1/0
except ZeroDivisionError:
    pass
except (ValueError, KeyError):
    pass
"""
        analyzer = PythonASTAnalyzer(bare_except=True, getattr_setattr=False, barrel_init=False)
        violations = analyzer.analyze_code(code, TEST_FILE)

        assert len(violations) == 0

    def test_disabled_check(self):
        """Test that check can be disabled."""
        code = """
try:
    x = 1/0
except:
    pass
"""
        analyzer = PythonASTAnalyzer(bare_except=False, getattr_setattr=False, barrel_init=False)
        violations = analyzer.analyze_code(code, TEST_FILE)

        assert len(violations) == 0


class TestGetAttrSetAttr:
    """Test hasattr/getattr/setattr detection."""

    def test_detects_hasattr(self):
        """Test that hasattr is detected."""
        code = """
obj = object()
if hasattr(obj, 'foo'):
    print("has foo")
"""
        analyzer = PythonASTAnalyzer(bare_except=False, getattr_setattr=True, barrel_init=False)
        violations = analyzer.analyze_code(code, TEST_FILE)

        assert len(violations) == 1
        assert violations[0].line == 3
        assert "hasattr" in violations[0].message
        assert violations[0].rule == "getattr_setattr"

    def test_detects_getattr(self):
        """Test that getattr is detected."""
        code = """
obj = object()
value = getattr(obj, 'foo', 'default')
"""
        analyzer = PythonASTAnalyzer(bare_except=False, getattr_setattr=True, barrel_init=False)
        violations = analyzer.analyze_code(code, TEST_FILE)

        assert len(violations) == 1
        assert violations[0].line == 3
        assert "getattr" in violations[0].message

    def test_detects_setattr(self):
        """Test that setattr is detected."""
        code = """
obj = object()
setattr(obj, 'foo', 'bar')
"""
        analyzer = PythonASTAnalyzer(bare_except=False, getattr_setattr=True, barrel_init=False)
        violations = analyzer.analyze_code(code, TEST_FILE)

        assert len(violations) == 1
        assert violations[0].line == 3
        assert "setattr" in violations[0].message

    def test_allows_regular_attributes(self):
        """Test that regular attribute access is allowed."""
        code = """
obj = object()
obj.foo = 'bar'
value = obj.foo
# Direct attribute access should be fine
if hasattr(obj, 'foo'):  # But this is still the hasattr function
    pass
"""
        analyzer = PythonASTAnalyzer(bare_except=False, getattr_setattr=True, barrel_init=False)
        violations = analyzer.analyze_code(code, TEST_FILE)

        assert len(violations) == 1  # Should still catch hasattr
        assert violations[0].line == 6

    def test_allows_attribute_access(self):
        """Test that normal attribute access is allowed."""
        code = """
class MyClass:
    def __init__(self):
        self.foo = 'bar'

obj = MyClass()
obj.bar = 'baz'
value = obj.foo
del obj.bar
"""
        analyzer = PythonASTAnalyzer(bare_except=False, getattr_setattr=True, barrel_init=False)
        violations = analyzer.analyze_code(code, TEST_FILE)

        assert len(violations) == 0


class TestBarrelInit:
    """Test barrel __init__.py detection."""

    def test_detects_star_import(self):
        """Test that star imports in __init__.py are detected."""
        code = """
from .module1 import *
from .module2 import something
"""
        analyzer = PythonASTAnalyzer(bare_except=False, getattr_setattr=False, barrel_init=True)
        violations = analyzer.analyze_code(code, Path("__init__.py"))

        assert len(violations) == 1
        assert violations[0].line == 2
        assert "star import" in violations[0].message.lower()
        assert violations[0].rule == "barrel_init"

    def test_detects_reexport_pattern(self):
        """Test that re-export patterns are detected."""
        code = """
from .module1 import Class1, Class2
from .module2 import Class3
from .module3 import helper_func

__all__ = ['Class1', 'Class2', 'Class3', 'helper_func']
"""
        analyzer = PythonASTAnalyzer(bare_except=False, getattr_setattr=False, barrel_init=True)
        violations = analyzer.analyze_code(code, Path("__init__.py"))

        assert len(violations) == 1
        assert "barrel" in violations[0].message.lower()
        assert violations[0].rule == "barrel_init"

    def test_allows_minimal_init(self):
        """Test that minimal __init__.py files are allowed."""
        code = """
# Package initialization
__version__ = "1.0.0"
"""
        analyzer = PythonASTAnalyzer(bare_except=False, getattr_setattr=False, barrel_init=True)
        violations = analyzer.analyze_code(code, Path("__init__.py"))

        assert len(violations) == 0

    def test_ignores_non_init_files(self):
        """Test that barrel pattern is only checked in __init__.py."""
        code = """
from .module1 import *
from .module2 import Class1
__all__ = ['Class1']
"""
        analyzer = PythonASTAnalyzer(bare_except=False, getattr_setattr=False, barrel_init=True)
        violations = analyzer.analyze_code(code, Path("regular_file.py"))

        assert len(violations) == 0


class TestMultipleViolations:
    """Test detection of multiple violations."""

    def test_multiple_violations(self):
        """Test that multiple violations are all detected."""
        code = """
try:
    if hasattr(obj, 'foo'):
        value = getattr(obj, 'foo')
except:
    pass
"""
        analyzer = PythonASTAnalyzer(bare_except=True, getattr_setattr=True, barrel_init=False)
        violations = analyzer.analyze_code(code, TEST_FILE)

        assert len(violations) == 3
        # Should find: bare except, hasattr, getattr
        rules = {v.rule for v in violations}
        assert "bare_except" in rules
        assert "getattr_setattr" in rules


class TestSyntaxErrors:
    """Test handling of syntax errors."""

    def test_syntax_error_handling(self):
        """Test that syntax errors are reported properly."""
        code = """
def foo(
    # Missing closing paren
"""
        analyzer = PythonASTAnalyzer()
        violations = analyzer.analyze_code(code, TEST_FILE)

        assert len(violations) == 1
        assert violations[0].rule == "syntax"
        assert "syntax error" in violations[0].message.lower()
