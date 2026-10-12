from server.chunker import Chunker
from server.langid import LangId
from server.prompts import SYSTEM_PROMPT, build_messages
from server.textclean import clean_for_tts, is_echo, is_hallucination


def chunk_stream(text: str, step: int = 3) -> list[str]:
    c = Chunker()
    out = []
    for i in range(0, len(text), step):
        out += c.feed(text[i : i + step])
    return out + c.flush()


# -- chunker ---------------------------------------------------------------------------


def test_sentences_italian():
    out = chunk_stream("Ciao! Oggi a Milano fa bel tempo e il sole splende. Vuoi uscire a fare una passeggiata nel parco?")
    assert out[-1] == "Vuoi uscire a fare una passeggiata nel parco?"
    assert "".join(out).replace(" ", "") == "Ciao!OggiaMilanofabeltempoeilsolesplende.Vuoiuscireafareunapasseggiatanelparco?"


def test_short_first_sentence_merges():
    out = chunk_stream("Sì. Certo che posso aiutarti con questa richiesta oggi.")
    assert out[0].startswith("Sì. Certo")


def test_eager_first_chunk_at_comma():
    out = chunk_stream("Dipende molto dai tuoi interessi personali, ma ti consiglio Siena e la Val d'Orcia. Poi anche Lucca.")
    assert out[0] == "Dipende molto dai tuoi interessi personali,"


def test_eager_first_chunk_long_without_punctuation():
    text = "questa è una frase molto lunga senza alcuna punteggiatura che continua e continua ancora per un po"
    out = chunk_stream(text)
    assert len(out[0]) <= 61 and len(out) >= 2


def test_abbreviations_decimals_times():
    out = chunk_stream("Ho parlato con il Dott. Rossi alle 10:30 di ieri mattina. Il valore era 3.5 per cento, mi pare. Ok.")
    assert out[0].startswith("Ho parlato con il Dott. Rossi alle 10:30")
    assert any("3.5 per cento" in o for o in out)


def test_english_abbrev():
    out = chunk_stream("I met Mr. Smith yesterday at the station near the park. It was e.g. fine today, really.")
    assert out[0] == "I met Mr. Smith yesterday at the station near the park."


def test_flush_without_punctuation():
    assert chunk_stream("ciao come stai") == ["ciao come stai"]


def test_hard_limit():
    c = Chunker()
    c.feed("Prima frase completa qui, bene.")  # emits first chunk
    out = c.feed("parola " * 60)
    assert out and all(len(o) <= 200 for o in out)


# -- textclean ---------------------------------------------------------------------------


def test_clean_markdown_emoji_urls():
    assert clean_for_tts("**Ciao** 👋 vedi [qui](https://x.it) e https://example.com") == "Ciao vedi qui e"
    assert clean_for_tts("- primo punto") == "primo punto"
    assert clean_for_tts("## Titolo") == "Titolo"
    assert clean_for_tts("😀 !!") == ""


def test_hallucinations():
    assert is_hallucination("Sottotitoli creati dalla comunità Amara.org")
    assert is_hallucination("Thanks for watching!")
    assert not is_hallucination("Grazie per l'aiuto")


def test_echo_guard():
    bot = ["Ti consiglio assolutamente la Val d'Orcia, specialmente Pienza.", "È un paesaggio meraviglioso."]
    assert is_echo("specialmente Pienza è un paesaggio", bot)
    assert is_echo("ti consiglio assolutamente la val dorcia", bot)
    assert not is_echo("e per mangiare cosa mi consigli", bot)
    assert not is_echo("ok", bot)


# -- langid ------------------------------------------------------------------------------

lid = LangId()


def test_langid_sticky_short():
    assert lid.resolve("sì", "it") == "it"
    assert lid.resolve("ok", "en") == "en"
    assert lid.resolve("yes sure", "it") == "it"


def test_langid_switch_long():
    assert lid.resolve("Hello, how are you doing today my friend?", "it") == "en"
    assert lid.resolve("Bonjour, comment allez-vous aujourd'hui mon ami ?", "it") == "fr"
    assert lid.resolve("Ciao, come stai oggi amico mio?", "en") == "it"


def test_langid_italian_questions():
    for q in ["Mi puoi dire che tempo fa domani a Roma?", "Raccontami una barzelletta divertente", "quanto costa un caffè al bar"]:
        assert lid.resolve(q, "en") == "it", q


def test_langid_loanwords_stay():
    assert lid.resolve("ho una call e un meeting domani mattina", "it") == "it"


def test_langid_stt_label():
    assert lid.resolve("whatever text here now", "it", stt_lang="de") == "de"


# -- prompts -----------------------------------------------------------------------------


def test_build_messages_language_last_and_history_untouched():
    history = [
        {"role": "user", "content": "ciao"},
        {"role": "assistant", "content": "ciao!"},
        {"role": "user", "content": "hello there"},
    ]
    msgs = build_messages(history, "en")
    assert msgs[0] == {"role": "system", "content": SYSTEM_PROMPT}
    assert msgs[-1]["content"].endswith("(Reply in English.)")
    assert history[-1]["content"] == "hello there"  # not mutated
    assert msgs[1]["content"] == "ciao"  # earlier prefix unchanged (cache-friendly)


def test_build_messages_last_turns():
    history = []
    for i in range(10):
        history += [{"role": "user", "content": f"u{i}"}, {"role": "assistant", "content": f"a{i}"}]
    history.append({"role": "user", "content": "now"})
    msgs = build_messages(history, "it", last_turns=2)
    assert [m["content"].split("\n")[0] for m in msgs[1:]] == ["u8", "a8", "u9", "a9", "now"]


# -- regressions from code review ---------------------------------------------------------


def test_echo_guard_keeps_short_real_answers():
    bot = ["Preferisci il mare o la montagna?", "Posso darti qualche idea per entrambe."]
    for answer in ["la montagna", "il mare", "sì il mare", "preferisco il mare non la montagna"]:
        assert not is_echo(answer, bot), answer
    assert is_echo("posso darti qualche idea per", bot)
    assert is_echo("Preferisci il mare o la montagna", bot)


def test_clean_keeps_math_identifiers_and_units():
    assert clean_for_tts("Il risultato di 3 * 4 è dodici") == "Il risultato di 3 per 4 è dodici"
    assert clean_for_tts("3 * 4", "en") == "3 times 4"
    assert clean_for_tts("apri my_config_file") == "apri my_config_file"
    assert clean_for_tts("Oggi ci sono 25°C a Roma.") == "Oggi ci sono 25 gradi Celsius a Roma."
    assert clean_for_tts("It is 70 °F", "en") == "It is 70 degrees Fahrenheit"
    assert clean_for_tts("**grassetto** e _corsivo_") == "grassetto e corsivo"


def test_explicit_language_request():
    assert lid.requested("parliamo in inglese") == "en"
    assert lid.requested("rispondimi in francese per favore") == "fr"
    assert lid.requested("can you speak to me in Italian") == "it"
    assert lid.requested("come si dice gatto in inglese?") is None
    assert lid.requested("parlami del cinema francese") is None


def test_chunker_sentence_final_common_words_and_elision():
    assert chunk_stream("Ciao, sto bene grazie. Purtroppo la risposta è no. Però posso aiutarti, se vuoi.")[1] == "Purtroppo la risposta è no."
    out = chunk_stream("Guarda la pagina n. 5 del manuale, poi dimmi. Va bene?")
    assert any("pagina n. 5 del manuale" in o for o in out)


def test_chunker_newline_list_items():
    out = chunk_stream("Ecco tre idee per te oggi:\nvisitare Siena\nandare al mare\n")
    assert out == ["Ecco tre idee per te oggi:", "visitare Siena", "andare al mare"]


def test_language_question_or_negation_is_not_a_request():
    lid = LangId()
    assert lid.requested("Perché mi parli in inglese?") is None
    assert lid.requested("non parlarmi in inglese") is None
    assert lid.requested("why do you speak English") is None
    assert lid.requested("No, no, parlami in italiano.") == "it"
    assert lid.requested("Can you speak English please") == "en"


def test_foreign_text_while_italian_is_echo_candidate():
    lid = LangId()
    for echo in ["String.", "And look at it.", "Thank you."]:
        assert lid.foreign(echo, "it"), echo
    for real in ["va bene", "e poi?", "stop", "ok", "aspetta un attimo", "spegni tutto", "parliamo in inglese"]:
        assert not lid.foreign(real, "it"), real
