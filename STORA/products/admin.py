from django.contrib import admin

from STORA.products.models import Category, Product, Barcode, Suppliers, ProductSupplier

class BarcodeInProductsAdmin(admin.TabularInline):
    model = Barcode
    fields = ['code', 'position']
    extra = 1

class ProductSupplierInline(admin.TabularInline):
    model = ProductSupplier
    fk_name = 'product'
    fields = ['supplier', 'position']
    extra = 1

@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    # filter_horizontal doesn't support a through= M2M (the field carries
    # extra data -- position -- that a plain widget can't represent), so
    # suppliers are managed via the inline instead, same as barcodes.
    inlines = [BarcodeInProductsAdmin, ProductSupplierInline]
    list_display = ('internal_code', 'name', 'category', 'delivery_price', 'sell_price')
    search_fields = ('internal_code', 'name',)

admin.site.register(Category)

admin.site.register(Suppliers)


