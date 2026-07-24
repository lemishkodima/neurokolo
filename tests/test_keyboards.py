from club_bot.bot.keyboards import main_menu
from club_bot.services.admin import MenuLabels


def test_main_menu_shows_subscription_status_button() -> None:
    labels = MenuLabels(
        about="Про клуб",
        join="Доєднатися",
        subscription="Моя підписка",
        materials="Матеріали",
        support="Техпідтримка",
    )

    keyboard = main_menu(labels)
    button_texts = [
        button.text
        for row in keyboard.keyboard
        for button in row
    ]

    assert button_texts == [
        "Про клуб",
        "Доєднатися",
        "Моя підписка",
        "Матеріали",
        "Скасувати підписку ❌",
        "Техпідтримка",
    ]
    assert labels.subscription in button_texts
