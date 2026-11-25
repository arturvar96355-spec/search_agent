

def test_a_real_key_is_never_sent_to_a_middleman():
    """Настоящий ключ Anthropic не должен уходить чужому сервису.

    Ключ вида «sk-ant-…» работает только на api.anthropic.com. Если при нём в
    настройках остался адрес посредника, это не «другой шлюз», а отправка
    ключа целиком постороннему: он увидит его и сможет тратить деньги со счёта.
    """
    from orbit.config import Settings

    real = Settings(
        anthropic_api_key="sk-ant-api03-настоящий",
        anthropic_base_url="https://models2.cldc.dev",
    )
    assert real.api_gateway is None

    # А ключ посредника без его адреса не работает вовсе — адрес нужен.
    proxied = Settings(
        anthropic_api_key="mykey_посредника",
        anthropic_base_url="https://models2.cldc.dev",
    )
    assert proxied.api_gateway == "https://models2.cldc.dev"
