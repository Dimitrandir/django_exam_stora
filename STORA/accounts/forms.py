from django.contrib.auth import get_user_model
from django.contrib.auth.forms import UserCreationForm
from django import forms
from django.utils.translation import gettext_lazy as _

from STORA.accounts.models import CompanyProfile

User = get_user_model()

class CustomUserCreationForm(UserCreationForm):
    class Meta:
        model = User
        fields = ('username', 'first_name', 'last_name', 'email', 'phone', 'role')
        labels = {
            'username': _('Username'),
            'first_name': _('First Name'),
            'last_name': _('Last Name'),
            'email': _('Email Address'),
            'phone': _('Phone Number'),
            'role': _('Position'),
        }

        widgets = {
            'username': forms.TextInput(attrs={'placeholder': _('Enter username')}),
            'email': forms.EmailInput(attrs={'placeholder': 'name@example.com'}),
            'first_name': forms.TextInput(attrs={'placeholder': _('First name')}),
            'last_name': forms.TextInput(attrs={'placeholder': _('Last name')}),
            'phone': forms.TextInput(attrs={'placeholder': '+359'}),
        }


class CompanyProfileForm(forms.ModelForm):
    class Meta:
        model = CompanyProfile
        fields = ['name', 'bulstat', 'vat_n', 'address']