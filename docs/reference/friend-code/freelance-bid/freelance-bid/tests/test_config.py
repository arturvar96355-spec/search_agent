

def test_a_real_key_is_never_sent_to_a_middleman():
    """Настоящий ключ Anthropic не должен уходить чужому сервису.

    Ключ вида «sk-ant-…» работает только на api.anthropic.com. Если при нём в
    настройках остался адрес посредника, это не «другой шлюз», а отправка
    ключа целиком постороннему: он увидит его и сможет тратить деньги со
    счёта. Такой адрес игнорируем.
    """
    from fbid.config import Settings

    settings = Settings(
        anthropic_api_key="sk-ant-api03-настоящий",
        anthropic_base_url="https://models2.cldc.dev",
    )
    assert settings.api_gateway is None


def test_a_middleman_key_still_goes_through_the_middleman():
    """Ключ посредника без его адреса не работает вовсе — адрес нужен."""
    from fbid.config import Settings

    settings = Settings(
        anthropic_api_key="mykey_посредника",
        anthropic_base_url="https://models2.cldc.dev",
    )
    assert settings.api_gateway == "https://models2.cldc.dev"


def test_no_middleman_by_default():
    from fbid.config import Settings

    assert Settings(anthropic_api_key="sk-ant-x", anthropic_base_url=None).api_gateway is None
    assert Settings(anthropic_api_key="mykey", anthropic_base_url="  ").api_gateway is None
