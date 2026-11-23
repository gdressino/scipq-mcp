package com.example.shop;

public class OrderDao {
    /** A JDOQL filter: names the field in a string. */
    public String openFilter() {
        return "state == :p";
    }

    public long weight(Order o) {
        return o.getState() + o.getTotal();
    }
}
