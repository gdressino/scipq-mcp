package com.example.shop;

public abstract class Hook {
    abstract void before();

    static Hook wired() {
        return new Hook() {
            @Override
            void before() {
            }
        };
    }

    void run() {
        before();
    }
}
