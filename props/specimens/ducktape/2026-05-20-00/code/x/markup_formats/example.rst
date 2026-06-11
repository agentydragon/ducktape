====================
reStructuredText Example
====================

.. contents:: Table of Contents
   :depth: 2

Text Formatting
===============

This is **bold** and this is *italic*.

Superscript: E=mc\ :sup:`2` and subscript: H\ :sub:`2`\ O

Lists
=====

* Unordered item
* Another item

  * Nested item

#. First (auto-numbered)
#. Second

Definition Lists
----------------

Term 1
    Definition of term 1

Term 2
    Definition of term 2

Links and Images
================

`Link text <https://example.com>`_

.. image:: image.png
   :alt: Alt text
   :width: 300px

.. figure:: diagram.png
   :alt: A diagram

   This is the figure caption.

Code
====

Inline ``code`` and blocks:

.. code-block:: python

    def hello():
        print("Hello, world!")

Tables
======

Simple table:

=====  =====
Name   Value
=====  =====
Alpha  1
Beta   2
=====  =====

Grid table (more control):

+-------+-------+
| Name  | Value |
+=======+=======+
| Alpha | 1     |
+-------+-------+
| Beta  | 2     |
+-------+-------+

Admonitions
===========

.. note::

   This is a note.

.. tip::

   This is a tip.

.. warning::

   This is a warning.

.. danger::

   This is dangerous!

Cross-references
================

See `Tables`_ for table examples.

.. _custom-anchor:

Custom Anchor Section
---------------------

Reference with :ref:`custom-anchor`.

Includes
========

.. include:: /path/to/file.rst

(Commented example - would include another file)

Substitutions (Variables)
=========================

.. |project| replace:: MyProject

The project is called |project|.

Footnotes
=========

Here's a sentence with a footnote [1]_.

.. [1] This is the footnote content.

Blockquotes
===========

    This is a blockquote.
    It can span multiple lines.

    -- Attribution

Roles and Directives
====================

rST is extensible via roles (inline) and directives (block):

.. math::

   \int_0^\infty e^{-x^2} dx = \frac{\sqrt{\pi}}{2}

:math:`E = mc^2` inline math.
