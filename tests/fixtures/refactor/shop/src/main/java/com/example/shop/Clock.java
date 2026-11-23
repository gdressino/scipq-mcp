package com.example.shop;

public class Clock {
    Thread start() {
        return new Thread(new Ticker());
    }
}
