"""Test modular configuration functionality."""

from ducktape_llm_common.claude_linter_v2.config.clean_models import ModularConfig, RuleConfig
from ducktape_llm_common.claude_linter_v2.config.loader import ConfigLoader
from ducktape_llm_common.claude_linter_v2.rule_registry import RuleRegistry


def test_modular_config_creation():
    """Test creating a modular config."""
    config = ModularConfig()

    # Check defaults
    assert config.version == "2.0"

    # By default, rules dict is empty (defaults come from registry)
    assert config.rules == {}

    # Check that we can get default rule configs from registry
    bare_except_rule = RuleRegistry.get_by_key("python.bare_except")
    assert bare_except_rule is not None
    assert bare_except_rule.default_blocks_pre is True
    assert bare_except_rule.default_blocks_stop is True


def test_modular_config_rule_lookup():
    """Test looking up rule configurations."""
    config = ModularConfig()

    # Test lookup for rule with defaults
    bare_except_config = config.get_rule_config("python.bare_except")
    assert bare_except_config is not None
    assert bare_except_config.enabled is True  # Default from registry
    assert bare_except_config.blocks_pre_hook is True  # Default from registry
    assert bare_except_config.blocks_stop_hook is True  # Default from registry

    # Test lookup for non-existent rule
    fake_config = config.get_rule_config("fake.rule")
    assert fake_config is None


def test_modular_config_rule_override():
    """Test overriding rule configurations."""
    config = ModularConfig()

    # Override a rule
    config.rules["python.bare_except"] = RuleConfig(
        enabled=False, blocks_pre_hook=False, blocks_stop_hook=True, message="Custom message"
    )

    # Check the override
    bare_except_config = config.get_rule_config("python.bare_except")
    assert bare_except_config is not None
    assert bare_except_config.enabled is False
    assert bare_except_config.blocks_pre_hook is False
    assert bare_except_config.blocks_stop_hook is True
    assert bare_except_config.message == "Custom message"


def test_modular_config_ruff_codes():
    """Test getting ruff codes to select."""
    config = ModularConfig()

    # Get default ruff codes
    ruff_codes = config.get_ruff_codes_to_select()

    # Should include codes from registry that are enabled by default
    assert "E722" in ruff_codes  # bare except
    assert "BLE001" in ruff_codes  # blind except
    assert "B009" in ruff_codes  # getattr with constant
    assert "B010" in ruff_codes  # setattr with constant

    # Disable a rule
    config.rules["ruff.E722"] = RuleConfig(enabled=False)
    ruff_codes = config.get_ruff_codes_to_select()
    assert "E722" not in ruff_codes


def test_modular_config_save_load(tmp_path):
    """Test saving and loading modular config."""
    config_path = tmp_path / "test-modular.toml"

    # Create config with custom values
    config = ModularConfig()
    config.rules["python.bare_except"] = RuleConfig(enabled=False, message="Custom message")
    config.rules["ruff.E722"] = RuleConfig(blocks_stop_hook=False)

    # Save
    config.save_to_file(config_path)

    # Load
    loaded = ModularConfig.from_toml(config_path)

    bare_except_rule = loaded.rules["python.bare_except"]
    assert bare_except_rule.enabled is False
    assert bare_except_rule.message == "Custom message"

    e722_rule = loaded.rules["ruff.E722"]
    assert e722_rule.blocks_stop_hook is False


def test_modular_config_loading(tmp_path):
    """Test loading modular config from file."""
    # Create modular config file
    modular_path = tmp_path / "modular.toml"
    modular_content = """
version = "2.0"

[rules]
[rules."python.bare_except"]
enabled = false
message = "Custom message"

[rules."ruff.E722"]
enabled = true
blocks_stop_hook = false
"""
    modular_path.write_text(modular_content)

    # Test loading
    config = ModularConfig.from_toml(modular_path)
    assert config.version == "2.0"

    # Check loaded rules
    bare_except_config = config.get_rule_config("python.bare_except")
    assert bare_except_config is not None
    assert bare_except_config.enabled is False
    assert bare_except_config.message == "Custom message"

    e722_config = config.get_rule_config("ruff.E722")
    assert e722_config is not None
    assert e722_config.enabled is True
    assert e722_config.blocks_stop_hook is False


def test_config_loader_integration(tmp_path, monkeypatch):
    """Test ConfigLoader with ModularConfig."""
    # Create config file
    config_path = tmp_path / ".claude-linter.toml"
    config_content = """
version = "2.0"

[rules]
[rules."python.hasattr"]
enabled = false
blocks_pre_hook = false
"""
    config_path.write_text(config_content)

    # Change to tmp directory using pytest's monkeypatch
    monkeypatch.chdir(tmp_path)

    # Test loading
    loader = ConfigLoader()
    config = loader.config

    assert isinstance(config, ModularConfig)
    assert config.version == "2.0"

    hasattr_config = config.get_rule_config("python.hasattr")
    assert hasattr_config is not None
    assert hasattr_config.enabled is False
    assert hasattr_config.blocks_pre_hook is False
