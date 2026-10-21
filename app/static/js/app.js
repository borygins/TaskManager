document.addEventListener("DOMContentLoaded", () => {
    document.querySelectorAll("[data-contact-form]").forEach((form) => {
        const kind = form.elements.activity_type;
        const result = form.elements.result;
        const nextDate = form.elements.next_activity_date;
        const update = () => {
            const meeting = kind.value === "Встреча";
            for (const option of result.options) {
                if (option.value === "Недозвон") option.disabled = meeting;
            }
            if (meeting && result.value === "Недозвон") result.value = "";
            nextDate.required = ["Недозвон", "Перенос на дату"].includes(result.value);
            nextDate.min = form.elements.activity_date.value;
        };
        [kind, result, form.elements.activity_date].forEach((input) => input.addEventListener("change", update));
        form.addEventListener("submit", () => {
            const button = form.querySelector('button[type="submit"]');
            button.disabled = true;
            button.textContent = "Сохраняем…";
        });
        window.addEventListener("pageshow", () => {
            const button = form.querySelector('button[type="submit"]');
            button.disabled = false;
            button.textContent = "Сохранить контакт";
        });
        update();
    });
    const cards = document.querySelectorAll(".client-card, .panel, .funnel-card, .stat-card");
    cards.forEach((card, index) => {
        card.animate(
            [
                { opacity: 0, transform: "translateY(8px)" },
                { opacity: 1, transform: "translateY(0)" }
            ],
            {
                duration: 260 + index * 20,
                fill: "both",
                easing: "ease-out"
            }
        );
    });
});
