"""Forms for guest-facing web flows."""

from __future__ import annotations

from django import forms
from django.contrib.auth import get_user_model, password_validation
from django.contrib.auth.forms import (
    AuthenticationForm,
    PasswordChangeForm,
    PasswordResetForm,
    SetPasswordForm,
)
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

    # Existence checks are handled in the view with a neutral response to avoid
    # account enumeration. Password and field validation remain form-level.


class GuestAuthenticationForm(AuthenticationForm):
    username = forms.EmailField(label=_("Email"), max_length=254, widget=forms.EmailInput(attrs={"autofocus": True}))

    error_messages = {
        **AuthenticationForm.error_messages,
        "invalid_login": _(
            "Please enter a correct email address and password. Note that both fields may be case-sensitive."
        ),
    }


class GuestPasswordResetForm(PasswordResetForm):
    email = forms.EmailField(label=_("Email"), max_length=254, widget=forms.EmailInput(attrs={"autocomplete": "email"}))

    def get_users(self, email):
        """Only allow password reset for non-staff guest users."""
        email_field_name = User.get_email_field_name()
        active_users = User._default_manager.filter(
            **{f"{email_field_name}__iexact": email, "is_active": True, "is_staff": False}
        )
        return (user for user in active_users if user.has_usable_password())


class GuestSetPasswordForm(SetPasswordForm):
    new_password1 = forms.CharField(
        label=_("New password"),
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
        strip=False,
    )
    new_password2 = forms.CharField(
        label=_("Confirm new password"),
        strip=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
    )


class GuestPasswordChangeForm(PasswordChangeForm):
    old_password = forms.CharField(
        label=_("Current password"),
        strip=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "current-password", "autofocus": True}),
    )
    new_password1 = forms.CharField(
        label=_("New password"),
        strip=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
    )
    new_password2 = forms.CharField(
        label=_("Confirm new password"),
        strip=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
    )


class GuestProfileForm(forms.Form):
    first_name = forms.CharField(label=_("First name"), max_length=150, required=False)
    last_name = forms.CharField(label=_("Last name"), max_length=150, required=False)
    phone = forms.CharField(label=_("Phone"), max_length=40, required=False)
    language = forms.ChoiceField(
        label=_("Preferred language"),
        choices=[("en", _("English")), ("pl", _("Polish"))],
        required=False,
    )
    marketing_consent = forms.BooleanField(label=_("Marketing consent"), required=False)


class GuestEmailChangeForm(forms.Form):
    new_email = forms.EmailField(label=_("New email address"), max_length=254)

    def __init__(self, *args, user=None, **kwargs):
        self.user = user
        super().__init__(*args, **kwargs)

    def clean_new_email(self) -> str:
        email = (self.cleaned_data.get("new_email") or "").strip().lower()
        current = ((getattr(self.user, "email", "") or "") if self.user else "").strip().lower()
        if email and email == current:
            raise ValidationError(_("Enter a different email address."), code="same_email")
        if email and User.objects.filter(email__iexact=email).exclude(pk=getattr(self.user, "pk", None)).exists():
            raise ValidationError(_("This email address cannot be used."), code="email_in_use")
        return email
