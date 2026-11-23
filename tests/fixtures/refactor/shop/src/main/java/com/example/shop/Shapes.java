package com.example.shop;

/** Overrides that call their superclass: a super.draw() is bound at compile time. */
public class Shapes {
    static class Shape {
        void draw() {
        }
    }

    static class Circle extends Shape {
        @Override
        void draw() {
            super.draw();
        }
    }

    static class Square extends Shape {
        @Override
        void draw() {
            super.draw();
        }
    }

    static void paint(Shape s) {
        s.draw();
    }
}
