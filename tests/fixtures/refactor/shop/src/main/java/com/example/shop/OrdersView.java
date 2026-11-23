package com.example.shop;

import com.vaadin.flow.router.Route;

@Route(value = "orders", layout = AppShell.class)
public class OrdersView {
    public OrdersView() {
        new Badge();
        new CountBadge();
    }
}
