{
    'name': "Generic Background Service (Tests)",
    'summary': (
        "Technical module that have to be used to test "
        "Generic Background Service module"
    ),
    'author': "Center of Research and Development",
    'website': "https://crnd.pro",
    'category': 'Hidden',
    'version': '18.0.0.2.2',
    'depends': [
        'generic_mixin',
        'generic_background_service',
    ],
    'data': [
        'security/ir.model.access.csv',
        'views/test_bg_service_compute_power.xml',
    ],
    'images': ['static/description/banner.png'],
    'license': 'LGPL-3',
}
