from rest_framework import serializers
from STORA.products.models import Product, Barcode


class BarcodeSerializer(serializers.ModelSerializer):
    class Meta:
        model = Barcode
        fields = ['id', 'code', 'position']


class ProductSerializer(serializers.ModelSerializer):
    barcode = BarcodeSerializer(many=True, read_only=True)

    class Meta:
        model = Product
        fields = [
            'id',
            'internal_code',
            'name',
            'delivery_price',
            'sell_price',
            'quantity',
            'barcode',
        ]