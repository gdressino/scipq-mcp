package com.example.shop;

public class Overloads {
    void m(String s, int k) {
    }

    void m(Object o, int k) {
    }

    void use() {
        m(null, 1);
        m("x", 2);
    }
}
