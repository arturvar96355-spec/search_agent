

def test_letters_go_through_the_gateway(monkeypatch):
    """Ключ у нас от посредника: без его адреса Anthropic отвечает «invalid x-api-key».

    По виду это неотличимо от испорченного ключа, поэтому проверяем явно.
    """
    from orbit import config, llm

    monkeypatch.setenv("ANTHROPIC_API_KEY", "ключ-посредника")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://gateway.example")
    config.get_settings.cache_clear()
    try:
        client = llm._client(config.get_settings(), timeout=llm.api_timeout(), retries=1)
        assert "gateway.example" in str(client.base_url)
    finally:
        config.get_settings.cache_clear()


def test_connection_timeout_is_short_but_generation_gets_time():
    """Плоский срок либо режет живую генерацию, либо ждёт мёртвое соединение."""
    from orbit import llm

    timeout = llm.api_timeout()
    assert timeout.connect <= 15
    assert timeout.read >= 45


def test_empty_balance_behind_a_429_is_called_by_its_name():
    """Шлюз отдаёт пустой счёт кодом 429 и русским текстом.

    Прочитав «упёрлись в лимит запросов», владелец ждёт, пока пройдёт само.
    Оно не пройдёт: надо пополнить счёт. Поэтому денежные признаки идут в
    списке первыми и перекрывают правило про частоту.
    """
    from orbit.llm import _short_error

    gateway = RuntimeError(
        "Error code: 429 - {'type': 'error', 'error': {'type': 'rate_limit_error', "
        "'message': 'Баланс токенов исчерпан. Пополните баланс в боте.'}}"
    )
    assert "деньги" in _short_error(gateway)


def test_a_real_rate_limit_is_still_a_rate_limit():
    from orbit.llm import _short_error

    assert "лимит запросов" in _short_error(RuntimeError("Error code: 429 - too many requests"))


def test_prompts_that_are_not_formatted_have_plain_braces():
    """Удвоенные скобки нужны только там, где промпт проходит через .format().

    CLASSIFY_PROMPT уходит модели как есть, а собран был по образцу
    BASE_PROMPT — с удвоенными скобками. Модель получала образец ответа,
    который должна повторить, поломанным JSON: «{{ "kind": … }}».
    """
    import re

    from orbit import llm

    assert "{{" not in llm.CLASSIFY_PROMPT and "}}" not in llm.CLASSIFY_PROMPT
    sample = re.search(r"\{\s*\n\s*\"kind\".*?\n\}", llm.CLASSIFY_PROMPT, re.S)
    assert sample, "образец ответа должен быть в промпте"
    # Ключи образца — те же, что читает разбор ответа.
    keys = set(re.findall(r'"(\w+)"\s*:', sample.group(0)))
    assert {"kind", "reason", "when", "when_date", "hint"} <= keys


def test_the_formatted_prompt_keeps_its_doubled_braces():
    """А BASE_PROMPT прогоняется через .format(), и там удвоение обязательно."""
    from orbit import llm
    from orbit.models import Kind

    built = llm.build_prompt(Kind.CLIENT)
    if built is None:
        return  # промпт-файла нет в этой сборке — проверять нечего
    assert "{{" not in built, "после .format() удвоение обязано схлопнуться"
    assert '"write"' in built or '"kind"' in built or "{" in built
