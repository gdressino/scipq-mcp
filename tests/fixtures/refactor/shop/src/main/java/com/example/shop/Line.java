package com.example.shop;

import javax.jdo.annotations.PersistenceCapable;
import javax.jdo.annotations.Persistent;

@PersistenceCapable(table = "LINES")
public class Line {
    @Persistent
    private Order order;

    private int quantity;

    public Order owner() {
        return order;
    }
}
