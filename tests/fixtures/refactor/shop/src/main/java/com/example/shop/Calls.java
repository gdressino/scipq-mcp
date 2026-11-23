package com.example.shop;

import static com.example.shop.Prices.round;

import java.util.List;

public class Calls {
    long copy(List<Order> orders, Order b) {
        orders.forEach(a -> a.setState(b.getState()));
        long n = orders.stream().map(Order::getState).count();
        return n + round(2.5);
    }
}
