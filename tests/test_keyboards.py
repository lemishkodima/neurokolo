from club_bot.bot.keyboards import main_menu
from club_bot.services.admin import MenuLabels


def test_main_menu_does_not_show_subscription_status_button() -> None:
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
        "Матеріали",
        "Техпідтримка",
        "Скасувати підписку ❌",
    ]
    assert labels.subscription not in button_texts
