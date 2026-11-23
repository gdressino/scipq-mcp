# language: es
@carrito
Característica: Contenido del carrito

  Escenario: Agregar cinco
    Dado an empty cart named "otro"
    Cuando I add 5 items
    Entonces the cart holds 5 items
