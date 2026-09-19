from settings import ComponentConfig, HttpConfig, ProvisionerSettings

TEST_SETTINGS = ProvisionerSettings(
    home_assistant_url="http://home-assistant.test:8123",
    client_id="https://home.test/",
    redirect_uri="https://home.test/",
    username="test-admin",
    display_name="Test Administrator",
    required_onboarding_steps=frozenset({"user", "core_config", "integration", "analytics"}),
    http_config=HttpConfig(
        server_host=["127.0.0.1"],
        server_port=8124,
        cors_allowed_origins=["https://cast.test"],
        use_x_forwarded_for=True,
        trusted_proxies=["127.0.0.1/32"],
        login_attempts_threshold=-1,
        ip_ban_enabled=True,
        ssl_profile="modern",
        use_x_frame_options=True,
    ),
    components=(
        ComponentConfig(
            version="2.2.1",
            url="https://example.test/component.zip",
            sha256="0" * 64,
            archive_path="*/custom_components/example",
            install_dir="example",
            manifest_domain="example",
        ),
        ComponentConfig(
            version="1.1.1",
            url="https://example.test/root-component.zip",
            sha256="0" * 64,
            archive_path=".",
            install_dir="root_component",
            manifest_domain=None,
            config_files=("automations.yaml", "scripts.yaml", "scenes.yaml"),
        ),
    ),
    onboarding_enabled=True,
)
