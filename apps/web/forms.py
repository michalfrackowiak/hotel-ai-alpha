"""Forms for guest-facing web flows."""

from __future__ import annotations

from django import forms
from django.contrib.auth import get_user_model, password_validation
from django.contrib.auth.forms import AuthenticationForm
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

User = get_user_model()


class GuestRegistrationForm(forms.Form):
    email = forms.EmailField(label=_("Email"), max_length=254)
    first_name = forms.CharField(label=_("First name"), max_length=80)
    last_name = forms.CharField(label=_("Last name"), max_length=80, required=False)
    password1 = forms.CharField(
        label=_("Password"),
        strip=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
    )
    password2 = forms.CharField(
        label=_("Confirm password"),
        strip=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
    )

    def clean_email(self) -> str:
        return (self.cleaned_data.get("email") or "").strip().lower()

    def clean_password2(self) -> str:
        password1 = self.cleaned_data.get("password1")
        password2 = self.cleaned_data.get("password2")
        if password1 and password2 and password1 != password2:
            raise ValidationError(_("The two password fields didn't match."), code="password_mismatch")
        return password2

    def _post_clean(self) -> None:
        super()._post_clean()
        password = self.cleaned_data.get("password2")
        if password:
            user = User(
                username=self.cleaned_data.get("email", ""),
                email=self.cleaned_data.get("email", ""),
                first_name=self.cleaned_data.get("first_name", ""),
                last_name=self.cleaned_data.get("last_name", ""),
            )
            try:
                password_validation.validate_password(password, user)
            except ValidationError as exc:
                self.add_error("password2", exc)

    def clean(self):
        cleaned = super().clean()
        email = cleaned.get("email")
        if email and User.objects.filter(email__iexact=email).exists():
            # Avoid account enumeration; registration flow can show a neutral
            # follow-up, but keep validation usable for now.
            raise ValidationError(_("An account with this email address already exists."), code="email_exists")
        return cleaned


class GuestAuthenticationForm(AuthenticationForm):
    username = forms.EmailField(label=_("Email"), max_length=254, widget=forms.EmailInput(attrs={"autofocus": True}))

    error_messages = {
        **AuthenticationForm.error_messages,
        "invalid_login": _(
            "Please enter a correct email address and password. Note that both fields may be case-sensitive."
        ),
    }
